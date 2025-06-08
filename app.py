#Libraries
import os
import pandas as pd
import requests
from langchain.prompts import ChatPromptTemplate
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage, AIMessage
from langchain.tools import tool
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Annotated
from openai import OpenAI
from google.cloud import storage, firestore_v1
from google.oauth2 import service_account
from langsmith import traceable
from langchain_elasticsearch import ElasticsearchStore
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request

# Model LLM
#Read OPENAI API
with open('keys/api_openai.txt', 'r') as file:
    api_openai = file.read()
os.environ['OPENAI_API_KEY'] = api_openai

#llm
llm = ChatOpenAI(model= "gpt-4.1-2025-04-14", temperature=0)

# Embedding Model
embeddings = OpenAIEmbeddings(model="text-embedding-3-large")

#Trace project with LangSmith
os.environ["LANGCHAIN_TRACING_V2"]="true"
os.environ["LANGSMITH_ENDPOINT"]="https://api.smith.langchain.com"
os.environ["LANGSMITH_API_KEY"]="<your_langsmith_apikey>"
os.environ["LANGSMITH_PROJECT"]="<your_project_name>"

# Tools
#Tiingo API
with open('keys/api_tiingo.txt', 'r') as file:
    api_tiingo = file.read()
#Alpha API
with open('keys/api_alpha.txt', 'r') as file:
    api_alpha = file.read()
#Gmail App_Pass
with open('keys/app_pass.txt', 'r') as file:
    app_pass = file.read()

#TTS OpenAI
def generate_speech_from_text(
    text: Annotated[str, "Your text response to the user's question"]):
  """
  Tool to generate a speech stored in mp3 format.
  """
  client = OpenAI()
  with client.audio.speech.with_streaming_response.create(
    model="gpt-4o-mini-tts",
    voice="echo",
    input=text,
    instructions="Speak in a cheerful and friendly tone.",
    ) as response:

    response.stream_to_file("output_speech/speech.mp3")

#Cloud Storage
@tool
def upload_mp3_file_to_cloud_storage():
  '''
  Tool to upload the generated mp3 file to Google Cloud Storage in order to make it public. The tool outputs the mp3 file's public url provided by Google Cloud Storage.
  '''
  # Initialize the client
  storage_client = storage.Client()
  # Get the bucket
  bucket = storage_client.bucket('audios_library')
  # Create a blob object from the filepath
  blob = bucket.blob("speech1")
  # Upload the file
  blob.upload_from_filename('output_speech/speech.mp3')
  mp3_file_url = blob.public_url

  return mp3_file_url

# Tiingo Stock Prices
def get_historical_stock_prices(
    symbol: Annotated[str, "Stock Ticker"],
    start_date: Annotated[str, "Start date to fetch stock prices. Format yyyy-mm-dd"],
    end_date: Annotated[str, "End date to fetch stock prices. Format yyyy-mm-dd"],):
    """
    Fetch historical stock prices from Tiingo API for a given ticker within a date range.
    """
    headers = {
        'Content-Type': 'application/json',
        'Authorization' : api_tiingo
        }
    ticker = symbol
    start_date = start_date
    end_date = end_date

    # create the URL
    url = f"https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate={start_date}&endDate={end_date}&token={api_tiingo}"

    # make API request
    response = requests.get(url, headers=headers)

    # parse prices from the response
    prices = response.json()

    # create df
    df = pd.DataFrame(prices)
    df = df[['date', 'close']]
    df['date'] = pd.to_datetime(df['date']).dt.date
    return df

#AlphaVantage News Search
def get_stock_news_articles(
    ticker: Annotated[str, "Stock Ticker"],
    time_from: Annotated[str, "Start date to fetch stock news. Format YYYYMMDDT0500. For example: 20241215T0500"],
    time_to: Annotated[str, "End date to fetch stock news. Format YYYYMMDDT0500. For example: 20241230T0500"]):
  '''
  Retrieve news articles for a given ticker within a date range.
  '''
  # create the url
  url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers={ticker}&apikey={api_alpha}&time_from={time_from}&time_to={time_to}"

  # make api request
  r = requests.get(url)
  data = r.json()

  # access news key
  news_items = data.get("feed", [])

  # filter news: The ticker should be within the ticker_sentiment key with a relevance score >= 0.40
  filtered_news = []
  for item in news_items:
    ticker_sentiments = item.get("ticker_sentiment", [])
    valid_ticker = any(
            ts.get("ticker") == ticker and float(ts.get("relevance_score", 0)) > 0.40
            for ts in ticker_sentiments
        )

    if valid_ticker:
      filtered_news.append(item)

    if len(filtered_news) >= 5:
      break

  return filtered_news

# RAG - Earnings Call
@traceable
def extract_earnings_call_information(question: Annotated[str, "The user's question. Always include the ticker, quarter, and year in the query."],
                          company: Annotated[str,"The stock ticker in uppercase."],
                          quarter: Annotated[str,"Possible values are 1, 2, 3, and 4"],
                          year:Annotated[str, "The year provided by the user. Format: YYYY"]):
    """
    Tool to extract company information, results, and analyses discussed in the earnings call for a given ticker, quarter, and year.
    """
    db = ElasticsearchStore(
          embedding=embeddings,
          es_url="<your_instance_url>",
          es_user="<user>",
          es_password="<password>",
          index_name="indx_project"
    )
    # metadata filtering
    results = db.similarity_search(
        question,
        filter={
            "bool": {
                "must": [
                {"match_phrase": {"metadata.company": company}},
                {"match_phrase": {"metadata.quarter": quarter}},
                {"match_phrase": {"metadata.year": year}}
            ]
            }
        },
        k=3
    )
    return "\n\n".join([r.page_content for r in results])

# GMAIL
def send_email_message(
    to_address: Annotated[str, "Recipient's email address"],
    subject: Annotated[str, "Email subject"],
    body: Annotated[str, "Email body"],):
  """
  Tool to send your response in html format to the user's email address. Include emojis in your response.
  """
  #MIME instance
  msg = MIMEMultipart()
  msg['From'] = '<sender_email>' #Sender's email
  msg['To'] = to_address
  msg['Subject'] = subject
  msg.attach(MIMEText(body, 'html'))

  server = smtplib.SMTP('smtp.gmail.com', 587)
  server.starttls()
  #Login
  server.login('<sender_email>', app_pass) #Sender's email
  #send email
  server.sendmail('<sender_email>', to_address,
             msg.as_string()
             )
  server.quit()

#Agent
#Init Firebase
credentials = service_account.Credentials.from_service_account_file("keys/firebase_key.json")
#Specify zone
client_options = {"api_endpoint": "northamerica-northeast1-firestore.googleapis.com"}
#Connect to DB
db = firestore_v1.Client(
    credentials=credentials,
    project="memory-agent-5b7d5",
    client_options=client_options,
    database="chathistory"
)

# Functions to save and read memory
def save_history(thread_id, messages):
    data = [{"role": "user" if isinstance(m, HumanMessage) else "ai", "content": m.content} for m in messages]
    db.collection("chat_history4").document(thread_id).set({"messages": data})

def load_history(thread_id):
    doc = db.collection("chat_history4").document(thread_id).get()
    if doc.exists:
        data = doc.to_dict()["messages"]
        return [HumanMessage(content=m["content"]) if m["role"] == "user" else AIMessage(content=m["content"]) for m in data]
    return []

#Rules file
with open('keys/rules.txt', 'r') as file:
    rules = file.read()

# Agent function
#Toolkit
toolkit = [generate_speech_from_text, upload_mp3_file_to_cloud_storage, get_historical_stock_prices, get_stock_news_articles, extract_earnings_call_information, send_email_message]
    
prompt = ChatPromptTemplate.from_messages(
      [
          ("system", rules),
      ("human", "{messages}"),
      ]
)

#agent
agent_executor = create_react_agent(llm, toolkit, prompt=prompt)

def generate_agent_response(user_input):
  response = agent_executor.invoke({"messages": user_input})
  # Access message
  return response["messages"][-1].content

# Initialize Flask app
app = Flask(__name__)

# Build endpoint
@app.route('/discover', methods=['GET'])
def main():
    user_input = request.args.get('msg')
    #Load history
    thread_id = "demo_user"
    messages = load_history(thread_id)
    #Add user message
    messages.append(HumanMessage(content=user_input))
    #Build chat conversation
    chat_text = ""
    for m in messages:
      role = "Human" if isinstance(m, HumanMessage) else "AI"
      chat_text += f"{role}: {m.content}\n"
    ai_reply = generate_agent_response(chat_text)
    # Add AI message
    messages.append(AIMessage(content=ai_reply))
    save_history(thread_id, messages)

    #return output
    return ai_reply

