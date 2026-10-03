"""Phase 5A: the Telegram transport workflow and its routing / rendering code (run with Node)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from channels import telegram_workflow as tw

PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT.parents[1]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# A Telegram bot token is "<bot id>:<35 characters>".
BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def _workflow() -> dict:
    return json.loads(tw.OUT.read_text(encoding="utf-8"))


def _nodes() -> dict:
    return {n["name"]: n for n in _workflow()["nodes"]}


def _node(js: str):
    """Runs ``js`` with the transport library loaded as ``t``; ``js`` prints one JSON value."""
    script = f"const t = require({json.dumps(str(tw.TRANSPORT_JS))});\n{js}"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def _call(fn: str, *args):
    return _node(f"console.log(JSON.stringify(t.{fn}(...{json.dumps(list(args), ensure_ascii=False)})));")


def _code_node(name: str, input_json, refs: dict = None):
    """Runs the generated Code node ``name`` the way n8n does: ``$input`` is its input, ``$(node)`` another node's
    output. Returns the node's output items."""
    code = _nodes()[name]["parameters"]["jsCode"]
    script = (f"const INPUT = {json.dumps(input_json, ensure_ascii=False)};\n"
              f"const REFS = {json.dumps(refs or {}, ensure_ascii=False)};\n"
              "const $input = { first: () => ({ json: INPUT }), all: () => [{ json: INPUT }] };\n"
              "const $ = (n) => ({ first: () => ({ json: REFS[n] }) });\n"
              f"const items = (function () {{\n{code}\n}})();\n"
              "console.log(JSON.stringify(items));")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def _update(text=None, chat_id=1001, chat_type="private", **message):
    m = {"message_id": 7, "date": 1790000000, "chat": {"id": chat_id, "type": chat_type},
         "from": {"id": chat_id, "is_bot": False, "first_name": "Test"}, **message}
    if text is not None:
        m["text"] = text
    return {"update_id": 500, "message": m}


# --- workflow: a thin transport in front of the existing Consultant ---------------------------------------------

def test_committed_workflow_is_generated():
    assert tw.OUT.read_text(encoding="utf-8") == tw.render(), "run: python -m channels.telegram_workflow"
    assert tw.main(["--check"]) == 0


def test_workflow_is_a_thin_transport():
    wf = _workflow()
    assert wf["name"] == "TV Consultant — Telegram" and "active" not in wf and "id" not in wf
    types = {n["type"] for n in wf["nodes"]}
    # no Agent, model, memory, tool, database or HTTP node: the Consultant workflow owns all of them
    assert types == {"n8n-nodes-base.telegramTrigger", "n8n-nodes-base.code", "n8n-nodes-base.if",
                     "n8n-nodes-base.telegram", "n8n-nodes-base.executeWorkflow"}
    assert not any("langchain" in t or "postgres" in t.lower() or "httpRequest" in t for t in types)
    trigger, = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.telegramTrigger"]
    assert trigger["parameters"]["updates"] == ["message"] and trigger["webhookId"]


def test_text_reaches_the_existing_consultant_workflow():
    nodes = _nodes()
    ask = nodes["Ask Consultant"]
    p = ask["parameters"]
    consultant_meta = json.loads((PROJECT / "workflows/ai-consultant.meta.json").read_text())
    assert p["source"] == "database" and "workflowJson" not in p          # the deployed workflow, not a copy
    assert p["workflowId"]["value"] == consultant_meta["remoteWorkflowId"] == "4d8mXFWGpS5P4t1L"
    assert p["workflowInputs"]["value"] == {"chatInput": "={{ $json.chatInput }}", "sessionId": "={{ $json.sessionId }}"}
    assert p["mode"] == "once" and p["options"]["waitForSubWorkflow"] is True
    # The Consultant's contract: its Execute Workflow Trigger takes exactly these inputs, and its memory and the
    # semantic guard key on sessionId.
    consultant = {n["name"]: n for n in json.loads((PROJECT / "workflows/ai-consultant.json").read_text())["nodes"]}
    trigger_inputs = consultant["When called by evaluation workflow"]["parameters"]["workflowInputs"]["values"]
    assert tuple(i["name"] for i in trigger_inputs) == tw.CONSULTANT_INPUTS
    assert consultant["Window memory (per session)"]["parameters"]["sessionKey"] == "={{ $json.sessionId }}"
    assert "conv={{ encodeURIComponent($json.sessionId) }}" in consultant["catalog"]["parameters"]["endpointUrl"]
    c = _workflow()["connections"]
    assert [x["node"] for x in c["Is text?"]["main"][0]] == ["Show typing", "Ask Consultant"]
    assert [x["node"] for x in c["Is text?"]["main"][1]] == ["Send notice"]
    assert nodes["Show typing"]["position"][1] < ask["position"][1]       # order v1: typing first
    assert nodes["Show typing"]["onError"] == "continueRegularOutput"


def test_answer_goes_back_to_the_same_chat_and_failures_are_answered():
    nodes, c = _nodes(), _workflow()["connections"]
    # both outputs of the Consultant call (answer, error) are rendered; Telegram's HTML rejection falls back to text
    assert nodes["Ask Consultant"]["onError"] == "continueErrorOutput"
    assert c["Ask Consultant"]["main"] == [[{"node": "Format reply", "type": "main", "index": 0}]] * 2
    assert c["Format reply"]["main"] == [[{"node": "Send reply", "type": "main", "index": 0}]]
    assert nodes["Send reply"]["onError"] == "continueErrorOutput"
    assert c["Send reply"]["main"] == [[], [{"node": "Send reply (plain text)", "type": "main", "index": 0}]]
    assert "$('Route update').first().json.chatId" in nodes["Format reply"]["parameters"]["jsCode"]
    assert nodes["Send reply"]["parameters"]["chatId"] == "={{ $json.chatId }}"
    assert nodes["Send reply (plain text)"]["parameters"]["chatId"] == "={{ $('Format reply').item.json.chatId }}"
    assert nodes["Send notice"]["parameters"]["chatId"] == "={{ $json.chatId }}"
    for name in ("Send reply", "Send reply (plain text)", "Send notice"):
        fields = nodes[name]["parameters"]["additionalFields"]
        assert fields["appendAttribution"] is False                       # no "sent automatically with n8n"
        assert fields.get("parse_mode") == ("HTML" if name == "Send reply" else None)


def test_credentials_are_references_to_the_existing_telegram_credential():
    for n in _workflow()["nodes"]:
        if n["type"].startswith("n8n-nodes-base.telegram"):
            assert n["credentials"] == {"telegramApi": tw.TELEGRAM_CREDENTIAL}
        else:
            assert "credentials" not in n
    assert set(tw.TELEGRAM_CREDENTIAL) == {"id", "name"}


def test_no_telegram_token_or_transport_workflow_id_in_the_repository():
    text = tw.OUT.read_text(encoding="utf-8")
    assert not BOT_TOKEN.search(text) and "api.telegram.org" not in text
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=REPO, capture_output=True,
                           text=True, check=True).stdout.split("\n")
    hits = []
    for f in filter(None, files):
        path = REPO / f
        if path.is_file() and path.stat().st_size < 5_000_000:
            content = path.read_text(encoding="utf-8", errors="ignore")
            if BOT_TOKEN.search(content) or re.search(r"api\.telegram\.org/bot\w", content):
                hits.append(f)
    assert hits == []
    # The trigger's webhook secret is derived from the workflow id and the (committed) node id: the id stays local.
    for local in ("projects/samsung-ai-consultant/workflows/telegram-transport.meta.json",
                  "tools/n8n-tool/backups/tv-consultant-telegram/x.json",
                  "tools/n8n-tool/exports/tv-consultant-telegram/sanitized.json"):
        assert subprocess.run(["git", "check-ignore", "-q", local], cwd=REPO).returncode == 0, local


# --- routing and session identity -------------------------------------------------------------------------------

@needs_node
def test_text_message_is_routed_with_a_chat_session():
    r = _call("routeUpdate", _update("  Нужен телевизор 65 дюймов  ", chat_id=424242))
    assert r == {"kind": "text", "chatId": 424242, "chatInput": "Нужен телевизор 65 дюймов", "sessionId": "tg:424242"}
    from consultant.mcp_server import TURN_KEY
    assert TURN_KEY.match(r["sessionId"])              # otherwise the Consultant would skip the semantic guard


@needs_node
def test_sessions_are_isolated_per_chat_and_stable_within_a_chat():
    r = _node("""
      const ids = [1, 2, 10, 11, 12, 101, 1001, 7000000001, 7000000002, 9007199254740991];
      const a1 = t.routeUpdate({ message: { chat: { id: 555, type: 'private' }, text: 'Нужен телевизор' } });
      const a2 = t.routeUpdate({ message: { chat: { id: 555, type: 'private' }, text: 'А для PS5?' } });
      const b1 = t.routeUpdate({ message: { chat: { id: 556, type: 'private' }, text: 'Нужен телевизор' } });
      console.log(JSON.stringify({ sessions: ids.map(t.sessionIdFor), a1, a2, b1 }));
    """)
    assert len(set(r["sessions"])) == len(r["sessions"])                  # no two chats share a session
    assert r["a1"]["sessionId"] == r["a2"]["sessionId"] == "tg:555"       # a follow-up reaches the same memory
    assert r["b1"]["sessionId"] == "tg:556" != r["a1"]["sessionId"]
    for routed in (r["a1"], r["a2"], r["b1"]):
        assert routed["chatInput"] not in routed["sessionId"]             # never derived from the text


@needs_node
@pytest.mark.parametrize("text", ["/start", "/start@tv_consultant_bot", "/start ref42", "/help", "/menu"])
def test_commands_get_the_description(text):
    r = _call("routeUpdate", _update(text))
    assert r["kind"] == "start" and r["chatId"] == 1001 and "AI-консультант по телевизорам" in r["reply"]


@needs_node
@pytest.mark.parametrize("message", [{"photo": [{"file_id": "x"}]}, {"sticker": {"file_id": "x"}},
                                     {"voice": {"file_id": "x"}}, {"text": ""}, {"text": "   \n "}, {"text": 5}])
def test_non_text_messages_get_a_text_only_notice(message):
    r = _call("routeUpdate", _update(**message))
    assert r["kind"] == "unsupported" and r["chatId"] == 1001 and "только текстовые" in r["reply"]


@needs_node
@pytest.mark.parametrize("update", [
    None, {}, "text", {"update_id": 1}, {"edited_message": _update("x")["message"]},
    {"callback_query": {"id": "1"}}, {"channel_post": {"chat": {"id": -100, "type": "channel"}, "text": "x"}},
    _update("x", chat_type="group", chat_id=-42), _update("x", chat_type="supergroup", chat_id=-10042),
    _update("x", chat_id=-5), _update("x", chat_id="1001"), _update("x", chat_id=1.5),
    {"message": {"text": "x"}}, {"message": {"chat": {"id": 7, "type": "private"}, "text": "x",
                                             "from": {"id": 7, "is_bot": True}}},
])
def test_other_updates_are_ignored_without_error(update):
    assert _call("routeUpdate", update) is None


@needs_node
def test_too_long_messages_are_not_sent_to_the_consultant():
    limit = _node("console.log(t.MAX_INPUT_CHARS)")
    assert _call("routeUpdate", _update("а" * limit))["kind"] == "text"
    r = _call("routeUpdate", _update("а" * (limit + 1)))
    assert r["kind"] == "too_long" and "chatInput" not in r and "sessionId" not in r


# --- user-facing copy -------------------------------------------------------------------------------------------

@needs_node
def test_user_facing_copy_is_neutral():
    text = _node("console.log(JSON.stringify(t.TEXT))")
    assert set(text) == {"start", "unsupported", "tooLong", "unavailable"}
    for s in list(text.values()) + [tw.WORKFLOW_NAME]:
        assert not re.search(r"(?i)samsung|самсунг|galaxy", s), s
    start = text["start"]
    assert start.startswith("Здравствуйте! Я AI-консультант по телевизорам.") and "ограниченным ассортиментом" in start
    for capability in ("подобрать", "бюджету, диагонали и характеристикам", "сравнить 2–4 модели", "характеристиках"):
        assert capability in start
    # only use cases the Consultant has (recommend_tvs: gaming, movies, sound, bright_room, ...); no live-price claim
    assert not re.search(r"(?i)спорт|актуальн|доставк|заказ|оформ", start)
    assert len(start) < 1000


# --- rendering the Agent's answer -------------------------------------------------------------------------------

@needs_node
@pytest.mark.parametrize("result", [{}, {"output": ""}, {"output": "  "}, {"error": "Workflow is not active"},
                                    {"output": None}, None])
def test_missing_answer_becomes_the_unavailable_notice(result):
    parts = _call("formatReply", result)
    assert len(parts) == 1 and parts[0]["text"].startswith("Не получилось подготовить ответ")


@needs_node
def test_markdown_is_rendered_as_telegram_html():
    md = ("### **Подбор**\n1. **Samsung 55\" QN80H** (модель `QE55QN80HAUXPY`) — 129 990 ₽, в наличии.\n"
          "- [Страница товара](https://galaxystore.ru/product/QE55QN80HAUXPY/?a=1&b=2)\n"
          "Цена <без скидки> & прочее, 2 * 3 = 6")
    part, = _call("formatReply", {"output": md})
    assert part["html"].split("\n") == [
        "<b>Подбор</b>",
        "1. <b>Samsung 55&quot; QN80H</b> (модель <code>QE55QN80HAUXPY</code>) — 129 990 ₽, в наличии.",
        '• <a href="https://galaxystore.ru/product/QE55QN80HAUXPY/?a=1&amp;b=2">Страница товара</a>',
        "Цена &lt;без скидки&gt; &amp; прочее, 2 * 3 = 6"]
    assert part["text"].split("\n") == [
        "Подбор", '1. Samsung 55" QN80H (модель QE55QN80HAUXPY) — 129 990 ₽, в наличии.',
        "• Страница товара (https://galaxystore.ru/product/QE55QN80HAUXPY/?a=1&b=2)", "Цена <без скидки> & прочее, 2 * 3 = 6"]


@needs_node
def test_html_tags_stay_balanced():
    md = "**a** **b ** c** `x` ** d [e](https://x.y) [f](javascript:alert(1)) <b>raw</b>\n### h **i**"
    part, = _call("formatReply", {"output": md})
    html = part["html"]
    for tag in ("b", "code", "a"):
        assert html.count(f"<{tag}>") + html.count(f"<{tag} ") == html.count(f"</{tag}>"), tag
    assert all(h.startswith("https://") for h in re.findall(r'href="([^"]*)"', html)) and "&lt;b&gt;raw&lt;/b&gt;" in html


@needs_node
def test_long_answers_are_split_under_the_telegram_limit():
    limit = _node("console.log(t.MAX_CHUNK_CHARS)")
    assert limit <= 4096
    paras = [f"{i}. " + "Телевизор с частотой 120 Гц. " * 20 for i in range(40)]
    md = "\n\n".join(paras) + "\n\n" + "x" * (limit + 50) + "\n\n😀" * 3
    parts = _call("formatReply", {"output": md})
    assert len(parts) > 2 and all(0 < len(p["text"]) <= limit for p in parts)
    joined = "".join(p["text"] for p in parts)
    assert joined.replace("\n", "") == md.replace("\n", "")               # nothing lost, nothing reordered
    single = _call("splitMessage", "😀" * 5, 3)                           # never cut a surrogate pair
    assert single == ["😀", "😀", "😀", "😀", "😀"]


# --- the generated Code nodes, run as n8n runs them -------------------------------------------------------------

@needs_node
def test_code_nodes_route_and_reply_to_the_originating_chat():
    a = _code_node("Route update", _update("Сравни первые два", chat_id=111))
    b = _code_node("Route update", _update("Нужен телевизор", chat_id=222))
    assert a == [{"json": {"kind": "text", "chatId": 111, "chatInput": "Сравни первые два", "sessionId": "tg:111"}}]
    assert b[0]["json"]["sessionId"] == "tg:222"
    assert _code_node("Route update", {"update_id": 9, "edited_message": {}}) == []          # ends the execution
    assert _code_node("Route update", _update("x", chat_type="group", chat_id=-1)) == []
    start = _code_node("Route update", _update("/start", chat_id=111))
    assert start[0]["json"]["kind"] == "start"
    out = _code_node("Format reply", {"output": "**QE65S90HAUXPY** — 149 990 ₽"}, {"Route update": a[0]["json"]})
    assert out == [{"json": {"chatId": 111, "html": "<b>QE65S90HAUXPY</b> — 149 990 ₽",
                             "text": "QE65S90HAUXPY — 149 990 ₽"}}]
    failed = _code_node("Format reply", {"error": "Workflow is not active and cannot be executed."},
                        {"Route update": b[0]["json"]})
    assert failed[0]["json"]["chatId"] == 222 and failed[0]["json"]["text"].startswith("Не получилось")
