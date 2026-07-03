import requests
import sys

BASE_URL = "http://108.137.15.61:9000"

class ChatClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session_id = None
        self.think_mode = None  # hanya dipakai di turn pertama

    def send(self, prompt: str) -> str:
        headers = {"Content-Type": "application/json"}

        if self.session_id:
            headers["X-Session-ID"] = self.session_id

        body = {
            "model": "qwen",
            "messages": [{"role": "user", "content": prompt}],
        }

        # hanya kirim think_mode kalau belum ada session
        if not self.session_id and self.think_mode:
            body["think_mode"] = self.think_mode

        try:
            r = requests.post(
                f"{self.base_url}/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=180,
            )
            r.raise_for_status()
        except Exception as e:
            return f"[ERROR] {e}"

        data = r.json()

        # update session
        self.session_id = (
            r.headers.get("X-Session-ID")
            or data.get("x_meta", {}).get("session_id")
            or self.session_id
        )

        return data["choices"][0]["message"]["content"]

    def new_session(self, think_mode: str = None):
        self.session_id = None
        self.think_mode = think_mode


def main():
    print("=== Qwen CLI Chat ===")
    print("Commands:")
    print("  /new [fast|auto|thinking]  -> mulai session baru")
    print("  /mode                      -> lihat mode sekarang")
    print("  /exit                      -> keluar")
    print("")

    client = ChatClient(BASE_URL)

    # pilih mode awal
    mode = input("Pilih think_mode (fast/auto/thinking) [default=fast]: ").strip()
    if mode not in ("fast", "auto", "thinking"):
        mode = "fast"

    client.new_session(mode)

    print(f"[INFO] Session baru dimulai dengan mode: {mode}\n")

    while True:
        try:
            prompt = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not prompt:
            continue

        # command handler
        if prompt.startswith("/"):
            parts = prompt.split()

            if parts[0] == "/exit":
                print("Bye!")
                break

            elif parts[0] == "/mode":
                print(f"[INFO] Current think_mode: {client.think_mode}")
                continue

            elif parts[0] == "/new":
                new_mode = parts[1] if len(parts) > 1 else "fast"
                if new_mode not in ("fast", "auto", "thinking"):
                    print("[ERROR] Mode harus: fast / auto / thinking")
                    continue

                client.new_session(new_mode)
                print(f"[INFO] New session dengan mode: {new_mode}")
                continue

            else:
                print("[ERROR] Command tidak dikenal")
                continue

        # kirim ke API
        print("AI: ", end="", flush=True)
        reply = client.send(prompt)
        print(reply)
        print()


if __name__ == "__main__":
    main()