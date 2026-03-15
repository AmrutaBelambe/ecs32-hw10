import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import streamlit as st

st.set_page_config(page_title="My AI Chat", layout="wide")

CHATS_DIR = Path("chats")
MEMORY_PATH = Path("memory.json")

MEMORY_FIELDS = {
    "name",
    "preferred_language",
    "communication_style",
    "interests",
    "favorite_topics",
    "dislikes",
    "location",
    "occupation",
}

KEY_ALIASES = {
    "interest": "interests",
    "interests": "interests",
    "hobby": "interests",
    "hobbies": "interests",
    "fav_topics": "favorite_topics",
    "favorite_topics": "favorite_topics",
    "favorite_topic": "favorite_topics",
    "topics": "favorite_topics",
    "language": "preferred_language",
    "preferredlanguage": "preferred_language",
    "communicationstyle": "communication_style",
    "style": "communication_style",
}

SENSITIVE_FIELDS = {"name", "location", "occupation"}
CUE_PATTERNS = {
    "name": ["my name is", "i am", "i'm", "call me"],
    "location": ["i live in", "i'm from", "i am from", "based in"],
    "occupation": ["i am a", "i'm a", "i work as", "my job is"],
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | None) -> datetime:
    if not value:
        return now_utc()
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return now_utc()
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def format_relative(ts: datetime) -> str:
    if not ts:
        return ""
    delta = now_utc() - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "Now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def chat_title(messages: list[dict]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "").strip()
            if content:
                return (content[:32] + "…") if len(content) > 32 else content
    return "New Chat"


def serialize_chat(chat: dict) -> dict:
    return {
        "id": chat.get("id"),
        "title": chat.get("title") or chat_title(chat.get("messages", [])),
        "created_at": chat.get("created_at", now_utc()).isoformat(),
        "updated_at": chat.get("updated_at", now_utc()).isoformat(),
        "messages": chat.get("messages", []),
    }


def save_chat(chat: dict) -> None:
    CHATS_DIR.mkdir(exist_ok=True)
    chat_path = CHATS_DIR / f"{chat['id']}.json"
    chat_path.write_text(json.dumps(serialize_chat(chat), indent=2), encoding="utf-8")


def delete_chat_file(chat_id: str) -> None:
    chat_path = CHATS_DIR / f"{chat_id}.json"
    if chat_path.exists():
        chat_path.unlink()


def load_chats() -> list[dict]:
    CHATS_DIR.mkdir(exist_ok=True)
    chats: list[dict] = []
    for file_path in CHATS_DIR.glob("*.json"):
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        chat_id = data.get("id") or file_path.stem
        messages = data.get("messages", [])
        title = data.get("title") or chat_title(messages)
        created_at = parse_ts(data.get("created_at"))
        updated_at = parse_ts(data.get("updated_at"))
        chats.append(
            {
                "id": chat_id,
                "title": title,
                "messages": messages,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
    chats.sort(key=lambda c: c.get("updated_at", now_utc()), reverse=True)
    return chats


def get_chat_index(chat_id: str) -> int | None:
    for idx, chat in enumerate(st.session_state["chats"]):
        if chat.get("id") == chat_id:
            return idx
    return None


def sort_chats_by_updated() -> None:
    st.session_state["chats"].sort(
        key=lambda c: c.get("updated_at", now_utc()), reverse=True
    )


def extract_stream_delta(payload: dict) -> str:
    choices = payload.get("choices", [])
    if not choices:
        return ""
    delta = choices[0].get("delta", {})
    return delta.get("content", "") or ""


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return {}
    raw = MEMORY_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_memory(memory: dict) -> None:
    MEMORY_PATH.write_text(json.dumps(memory, indent=2), encoding="utf-8")


def normalize_key(key: str) -> str:
    normalized = key.strip().lower().replace(" ", "_")
    normalized = normalized.replace("-", "_")
    normalized = normalized.replace("__", "_")
    if normalized in KEY_ALIASES:
        return KEY_ALIASES[normalized]
    if normalized in MEMORY_FIELDS:
        return normalized
    return normalized


def normalize_list_value(value: str) -> str:
    return value.strip()


def merge_list(existing: list, incoming: list) -> list:
    seen = set()
    merged = []
    for item in existing + incoming:
        if not isinstance(item, str):
            continue
        norm = item.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        merged.append(item.strip())
    return merged


def merge_memory(existing: dict, new_data: dict) -> dict:
    merged = dict(existing)
    for raw_key, value in new_data.items():
        if value in (None, ""):
            continue
        key = normalize_key(str(raw_key))
        if not key:
            continue
        if isinstance(value, list):
            current = merged.get(key, [])
            if not isinstance(current, list):
                current = [current] if current not in (None, "") else []
            normalized_incoming = [
                normalize_list_value(item)
                for item in value
                if isinstance(item, str) and item.strip()
            ]
            merged[key] = merge_list(current, normalized_incoming)
        else:
            if isinstance(value, str) and not value.strip():
                continue
            merged[key] = value
    return merged


def summarize_memory(memory: dict) -> str:
    lines = [f"{key}: {value}" for key, value in memory.items()]
    return "User preferences: " + "; ".join(lines)


def build_memory_prompt(user_message: str) -> list[dict]:
    fields = ", ".join(sorted(MEMORY_FIELDS))
    return [
        {
            "role": "system",
            "content": (
                "Extract only personal facts or preferences explicitly stated by the user. "
                "Do NOT infer, guess, or assume. Return only raw JSON. "
                "No markdown, no code fences, no extra text. "
                f"Use only these keys if relevant: {fields}. "
                "If none, return {}."
            ),
        },
        {"role": "user", "content": user_message},
    ]


def safe_parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 2)
        if len(parts) >= 2:
            cleaned = parts[1].strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}

    candidate = cleaned[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def matches_cue(field: str, user_message: str) -> bool:
    cues = CUE_PATTERNS.get(field, [])
    lowered = user_message.lower()
    return any(cue in lowered for cue in cues)


def value_in_message(value: str, user_message: str) -> bool:
    return value.strip().lower() in user_message.lower()


def filter_extracted_memory(extracted: dict, user_message: str) -> dict:
    filtered: dict = {}
    lowered = user_message.lower()

    for raw_key, value in extracted.items():
        key = normalize_key(str(raw_key))
        if not key:
            continue

        if isinstance(value, list):
            kept = []
            for item in value:
                if not isinstance(item, str):
                    continue
                if item.strip().lower() in lowered:
                    kept.append(item.strip())
            if kept:
                filtered[key] = kept
        else:
            if not isinstance(value, str):
                continue
            if not value.strip():
                continue
            if value_in_message(value, user_message):
                if key in SENSITIVE_FIELDS and not matches_cue(key, user_message):
                    continue
                filtered[key] = value.strip()

    return filtered


def extract_memory_from_message(hf_token: str, user_message: str) -> dict:
    headers = {"Authorization": f"Bearer {hf_token}"}
    payload = {
        "model": "meta-llama/Llama-3.2-1B-Instruct",
        "messages": build_memory_prompt(user_message),
        "max_tokens": 128,
    }

    try:
        response = requests.post(
            "https://router.huggingface.co/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=20,
        )
    except requests.exceptions.RequestException:
        return {}

    if response.status_code != 200:
        return {}

    try:
        data = response.json()
    except json.JSONDecodeError:
        return {}

    choices = data.get("choices", [])
    if not choices:
        return {}
    message = choices[0].get("message", {})
    content = message.get("content", "").strip()
    if not content:
        return {}

    extracted = safe_parse_json(content)
    if not extracted:
        return {}

    normalized = {}
    for raw_key, value in extracted.items():
        key = normalize_key(str(raw_key))
        if not key:
            continue
        normalized[key] = value

    return filter_extracted_memory(normalized, user_message)


if "chats" not in st.session_state:
    st.session_state["chats"] = load_chats()
if "active_chat_id" not in st.session_state:
    st.session_state["active_chat_id"] = None
if "memory" not in st.session_state:
    st.session_state["memory"] = merge_memory({}, load_memory())

if st.session_state["chats"] and st.session_state["active_chat_id"] is None:
    st.session_state["active_chat_id"] = st.session_state["chats"][0]["id"]


st.sidebar.title("Chats")
new_chat_clicked = st.sidebar.button("New Chat")

if new_chat_clicked:
    new_id = str(uuid.uuid4())
    chat = {
        "id": new_id,
        "title": "New Chat",
        "messages": [],
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    st.session_state["chats"].insert(0, chat)
    st.session_state["active_chat_id"] = new_id
    save_chat(chat)
    st.rerun()

if not st.session_state["chats"]:
    st.session_state["active_chat_id"] = None


with st.sidebar.container(height=460):
    for chat in st.session_state["chats"]:
        is_active = chat["id"] == st.session_state["active_chat_id"]
        title = chat.get("title") or chat_title(chat["messages"])
        stamp = format_relative(chat.get("updated_at"))

        row = st.container()
        cols = row.columns([0.82, 0.18])
        with cols[0]:
            label = f"▶ {title}" if is_active else title
            if st.button(label, key=f"chat-select-{chat['id']}"):
                st.session_state["active_chat_id"] = chat["id"]
                st.rerun()
            if stamp:
                st.caption(stamp)
        with cols[1]:
            if st.button("✕", key=f"chat-del-{chat['id']}"):
                current_id = st.session_state["active_chat_id"]
                st.session_state["chats"] = [
                    c for c in st.session_state["chats"] if c["id"] != chat["id"]
                ]
                delete_chat_file(chat["id"])
                if current_id == chat["id"]:
                    if st.session_state["chats"]:
                        st.session_state["active_chat_id"] = (
                            st.session_state["chats"][0]["id"]
                        )
                    else:
                        st.session_state["active_chat_id"] = None
                st.rerun()

with st.sidebar.expander("User Memory", expanded=True):
    st.json(st.session_state.get("memory", {}))
    if st.button("Clear Memory"):
        st.session_state["memory"] = {}
        save_memory({})
        st.rerun()


st.title("My AI Chat")

hf_token = st.secrets.get("HF_TOKEN", "")
if not hf_token:
    st.error("Missing HF token. Add HF_TOKEN to .streamlit/secrets.toml.")
    st.stop()

active_id = st.session_state["active_chat_id"]
active_idx = get_chat_index(active_id) if active_id else None
active_chat = (
    st.session_state["chats"][active_idx] if active_idx is not None else None
)

if not active_chat:
    st.info("No chats yet. Click New Chat to start a conversation.")
    st.stop()

chat_container = st.container(height=520)
with chat_container:
    for message in active_chat["messages"]:
        role = message.get("role", "assistant")
        content = message.get("content", "")
        with st.chat_message(role):
            st.write(content)

user_input = st.chat_input("Type a message and press Enter")

if user_input:
    active_chat["messages"].append({"role": "user", "content": user_input})
    if not active_chat.get("title") or active_chat.get("title") == "New Chat":
        active_chat["title"] = chat_title(active_chat["messages"])
    active_chat["updated_at"] = now_utc()
    save_chat(active_chat)
    sort_chats_by_updated()

    memory = st.session_state.get("memory", {})
    system_message = None
    if memory:
        system_message = {"role": "system", "content": summarize_memory(memory)}

    chat_messages = active_chat["messages"]
    if system_message:
        chat_messages = [system_message] + active_chat["messages"]

    headers = {"Authorization": f"Bearer {hf_token}"}
    payload = {
        "model": "meta-llama/Llama-3.2-1B-Instruct",
        "messages": chat_messages,
        "max_tokens": 512,
        "stream": True,
    }

    try:
        response = requests.post(
            "https://router.huggingface.co/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30,
            stream=True,
        )
    except requests.exceptions.RequestException as exc:
        st.error(f"Request failed: {exc}")
    else:
        if response.status_code != 200:
            st.error(
                f"API error {response.status_code}: {response.text.strip() or 'Unknown error'}"
            )
        else:
            streamed_text = ""
            with st.chat_message("assistant"):
                placeholder = st.empty()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue
                    data = line.removeprefix("data: ").strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload_chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta_text = extract_stream_delta(payload_chunk)
                    if not delta_text:
                        continue
                    streamed_text += delta_text
                    placeholder.write(streamed_text)
                    time.sleep(0.03)

            if not streamed_text.strip():
                st.error("The API response did not include message content.")
            else:
                active_chat["messages"].append(
                    {"role": "assistant", "content": streamed_text}
                )
                active_chat["updated_at"] = now_utc()
                save_chat(active_chat)
                sort_chats_by_updated()

                extracted = extract_memory_from_message(hf_token, user_input)
                if extracted:
                    st.session_state["memory"] = merge_memory(
                        st.session_state.get("memory", {}), extracted
                    )
                    save_memory(st.session_state["memory"])

                st.rerun()
