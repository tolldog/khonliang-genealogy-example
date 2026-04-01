#!/usr/bin/env python3
"""
CLI chat client — connects to the genealogy WebSocket server.

Usage:
    python -m genealogy_agent.chat_client
    python -m genealogy_agent.chat_client --url ws://localhost:8765

Commands:
    /search <query>   Search the knowledge base
    /history           Show conversation history
    /status            Show server status
    /quit              Disconnect
"""

import argparse
import asyncio
import json
import sys


async def run_client(url: str):
    """Connect to the chat server and run an interactive session."""
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        print("websockets package required: pip install websockets")
        sys.exit(1)

    print(f"Connecting to {url}...")

    try:
        async with connect(url) as ws:
            # Receive welcome message
            welcome = json.loads(await ws.recv())
            session_id = welcome.get("session_id", "?")
            roles = welcome.get("roles", [])
            print(f"Connected! Session: {session_id}")
            print(f"Available roles: {', '.join(roles)}")
            print("Type your question, or /search, /history, /quit\n")

            last_message_id = None

            while True:
                try:
                    user_input = input("You: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nDisconnected.")
                    break

                if not user_input:
                    continue

                if user_input.lower() in ("/quit", "/exit", "/q"):
                    print("Disconnected.")
                    break

                if user_input.lower() == "/history":
                    await ws.send(json.dumps({"type": "history"}))
                    resp = json.loads(await ws.recv())
                    for msg in resp.get("messages", []):
                        print(f"  [{msg['role']}] You: {msg['user'][:60]}")
                        print(f"           Agent: {msg['agent'][:80]}")
                    continue

                if user_input.lower().startswith("/search "):
                    query = user_input[8:].strip()
                    await ws.send(json.dumps({
                        "type": "search",
                        "query": query,
                    }))
                    resp = json.loads(await ws.recv())
                    results = resp.get("results", [])
                    if results:
                        print(f"  Found {len(results)} results:")
                        for r in results:
                            tier = {1: "AXIOM", 2: "IMPORTED", 3: "DERIVED"}.get(
                                r.get("tier"), "?"
                            )
                            print(
                                f"    [{tier}] {r['title']} "
                                f"(confidence: {r.get('confidence', 0):.0%})"
                            )
                            print(f"      {r['content'][:100]}")
                    else:
                        print("  No results.")
                    continue

                if user_input.lower().startswith("/rate "):
                    parts = user_input[6:].strip().split(None, 1)
                    if len(parts) >= 1 and last_message_id:
                        rating = int(parts[0])
                        await ws.send(json.dumps({
                            "type": "feedback",
                            "message_id": last_message_id,
                            "rating": rating,
                        }))
                        resp = json.loads(await ws.recv())
                        print("  Feedback recorded.")
                    else:
                        print("  Usage: /rate <1-5>")
                    continue

                # Regular chat message
                await ws.send(json.dumps({
                    "type": "message",
                    "content": user_input,
                }))

                resp = json.loads(await ws.recv())

                if resp.get("type") == "error":
                    print(f"  Error: {resp.get('content')}")
                    continue

                role = resp.get("role", "?")
                content = resp.get("content", "")
                reason = resp.get("reason", "")
                last_message_id = resp.get("message_id")

                print(f"\n[{role}] ({reason})")
                print(f"{content}\n")

    except ConnectionRefusedError:
        print(f"Could not connect to {url}. Is the server running?")
        sys.exit(1)
    except Exception as e:
        print(f"Connection error: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Genealogy chat client")
    parser.add_argument(
        "--url", default="ws://localhost:8765", help="WebSocket server URL"
    )
    args = parser.parse_args()
    asyncio.run(run_client(args.url))


if __name__ == "__main__":
    main()
