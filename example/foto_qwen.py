import base64, requests

# Encode gambar ke base64
with open("foto.jpg", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

requests.post("http://108.137.15.61:9000/v1/chat/completions", json={
    "model": "qwen",
    "messages": [{"role": "user", "content": "Apa yang ada di gambar ini?"}],
    "attachments": [
        {
            "filename": "foto.jpg",
            "data": b64,           # atau "data:image/png;base64,..." (Data URI)
            "mime_type": "image/png"
        }
    ]
})