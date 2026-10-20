"""Chat with Saathi in the terminal, without the phone app.

Usage: python cli.py --phone 919876543210
"""

import argparse

from app.agent.graph import handle_message
from app.agent.llm import get_llm
from app.agent.outbox import take
from app.db.session import get_session


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with Saathi locally.")
    parser.add_argument("--phone", required=True, help="a seeded user's phone, digits only")
    phone = parser.parse_args().phone
    llm = get_llm()
    print("Saathi CLI. Type 'exit' to quit.")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit"}:
            break
        if text:
            with get_session() as session:  # one transaction per message, like the app
                reply = handle_message(session, phone, text, llm)
                outbox = take(session)
            print("saathi>", reply)
            for item in outbox:  # the CLI never messages real people; it shows what would be delivered
                print(f"  [to {item['to']}]", item.get("text") or f"{item['kind']}: {item['file']}")


if __name__ == "__main__":
    main()
