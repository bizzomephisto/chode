import os
import requests
from typing import List
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

def chat_completion(prompt: str, system_message: str = "You are petey the chatbot.", history: List[dict] = None) -> str:
    if not API_KEY:
        return "Error: GEMINI_API_KEY is not set in your .env file."
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={API_KEY}"
    
    contents = []
    if history:
        for msg in history:
            role = "model" if msg.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})
            
    if prompt:
        contents.append({"role": "user", "parts": [{"text": prompt}]})
        
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_message}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.8
        }
    }
    
    models_to_try = [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-pro"
    ]
    
    last_err = None
    last_text = ""
    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={API_KEY}"
        try:
            response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=60)
            response.raise_for_status()
            data = response.json()
            # Success!
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except requests.exceptions.HTTPError as e:
            last_err = e
            last_text = response.text
            if response.status_code == 404:
                continue # Model not found, try the next one in the list
            # Return immediately if it's an auth or malformed request error (400, 401, 403)
            return f"Gemini API Error {response.status_code}: {response.text}"
        except Exception as e:
            return f"Error communicating with Gemini Chat API: {e}"
            
    return f"Failed to reach Gemini Chat (All models returned 404): {last_err} \n({last_text})"

def call_gemini(prompt: str, system_message: str = "You are petey the chatbot.", history: List[dict] = None) -> str:
    return chat_completion(prompt, system_message=system_message, history=history)

def get_embedding(text: str) -> List[float]:
    """Generates 768-dimensional embeddings using Gemini."""
    if not API_KEY or not text or not text.strip():
        return []
        
    models_to_try = [
        "gemini-embedding-2-preview",
        "gemini-embedding-2-flash",
        "gemini-embedding-001"
    ]
    
    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:embedContent?key={API_KEY}"
        
        payload = {
            "model": f"models/{model_name}",
            "content": {
                "parts": [{"text": text}]
            }
        }
        
        if "gemini-embedding-2" in model_name:
            payload["outputDimensionality"] = 768
        
        try:
            response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=60)
            response.raise_for_status()
            data = response.json()
            return data["embedding"]["values"]
        except requests.exceptions.HTTPError as e:
            if response.status_code == 404:
                print(f"[DEBUG] Model {model_name} returned 404: {response.text}")
                continue
            print(f"[ERROR] Gemini Embedding Auth/Request Error for {model_name}: {response.text}")
            return []
        except Exception as e:
            print(f"[ERROR] Gemini Embedding failed: {e}")
            return []
            
    print("[ERROR] Gemini Embedding failed: All models returning 404")
    return []
