import os

import requests

# Desactivamos los avisos de SSL por si acaso tu red intercepta la conexión
requests.packages.urllib3.disable_warnings()

url = "https://integrate.api.nvidia.com/v1/chat/completions"

api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
if not api_key:
    raise RuntimeError("Falta NVIDIA_API_KEY en variables de entorno.")

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
}

# Cambiamos al modelo Llama 3.1 70B (o Nemotron), que sí están activos en este endpoint
payload = {
    "model": "meta/llama-3.1-70b-instruct",
    "messages": [{"role": "user", "content": "Hola, responde en una frase."}],
    "temperature": 0.2,
    "top_p": 0.7,
    "max_tokens": 64
}

try:
    print("Enviando petición a NVIDIA...")
    response = requests.post(url, headers=headers, json=payload, verify=False, timeout=30)
    
    # Si devuelve error (404, 401, etc.) saltará al bloque except
    response.raise_for_status()
    
    resultado = response.json()
    print("\nRespuesta del modelo:")
    print(resultado['choices'][0]['message']['content'])

except requests.exceptions.HTTPError as e:
    print(f"\nError HTTP del servidor: {e}")
    print(f"Detalle devuelto por NVIDIA: {response.text}")
except Exception as e:
    print(f"\nError inesperado: {e}")