// Telegram transport of the TV consultant (Phase 5A): pure functions, no I/O.
//
// channels/telegram_workflow.py embeds this file into the Code nodes of workflows/telegram-transport.json, and
// tests/test_telegram_transport.py runs it with Node. The transport only routes a Telegram update to the existing
// Consultant workflow and renders its answer for Telegram; every catalog fact comes from the Consultant.

// User-facing copy. Neutral on purpose: the product is a TV consultant on a limited catalog, not a brand assistant.
const TEXT = {
  start:
    'Здравствуйте! Я AI-консультант по телевизорам.\n\n' +
    'Работаю с ограниченным ассортиментом — реальным каталогом телевизоров; цены и наличие беру из каталога. Могу:\n' +
    '• подобрать телевизор под задачи — кино, игры, звук, светлая комната;\n' +
    '• отфильтровать модели по бюджету, диагонали и характеристикам;\n' +
    '• сравнить 2–4 модели;\n' +
    '• ответить на вопросы о характеристиках моделей из каталога.\n\n' +
    'Напишите обычным текстом, что вы ищете, например: «Нужен телевизор 55 дюймов для игр». ' +
    'Я учитываю предыдущие сообщения этого чата.',
  unsupported: 'Пока я понимаю только текстовые сообщения. Напишите, пожалуйста, вопрос текстом.',
  tooLong: 'Сообщение слишком длинное. Сформулируйте, пожалуйста, вопрос короче — до 2000 символов.',
  unavailable: 'Не получилось подготовить ответ: сервис временно недоступен. Попробуйте, пожалуйста, ещё раз через минуту.',
};

const MAX_INPUT_CHARS = 2000;     // the message also travels in the Consultant's request URL (q=, guard context)
const MAX_CHUNK_CHARS = 3900;     // Telegram accepts up to 4096 characters per message

// One private chat = one conversation. A private chat id is the user's Telegram id: stable across messages and
// distinct per user. Only that number is used, never the message text.
function sessionIdFor(chatId) {
  return 'tg:' + chatId;
}

// -> null (ignored: not a private chat, not a message) or {kind, chatId, ...}.
//    kind 'text': {chatInput, sessionId} for the Consultant; any other kind: {reply} sent as is.
function routeUpdate(update) {
  const m = update && update.message;
  if (!m || !m.chat || m.chat.type !== 'private' || !Number.isSafeInteger(m.chat.id) || m.chat.id <= 0) return null;
  if (m.from && m.from.is_bot) return null;
  const chatId = m.chat.id;
  if (typeof m.text !== 'string' || !m.text.trim()) return { kind: 'unsupported', chatId, reply: TEXT.unsupported };
  const text = m.text.trim();
  // /start, /help and any other command: the bot has no commands besides its description.
  if (text.startsWith('/')) return { kind: 'start', chatId, reply: TEXT.start };
  if (text.length > MAX_INPUT_CHARS) return { kind: 'too_long', chatId, reply: TEXT.tooLong };
  return { kind: 'text', chatId, chatInput: text, sessionId: sessionIdFor(chatId) };
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// The Agent writes light Markdown (numbered lists, bare links, occasional **bold**, headings, `code`, [text](url)).
// Telegram's HTML mode accepts a few tags only; everything else is escaped. Each pattern opens and closes its tag
// within one line, so the tags are balanced.
function toTelegramHtml(md) {
  return md.split('\n').map((line) => {
    const heading = /^\s{0,3}#{1,6}\s+(.*)$/.exec(line);
    let s = escapeHtml(heading ? heading[1].replace(/\*\*/g, '') : line);
    s = s.replace(/^(\s*)[-*]\s+/, '$1• ');
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2">$1</a>');
    s = s.replace(/\*\*(?=\S)(.+?)(?<=\S)\*\*/g, '<b>$1</b>');
    return heading ? '<b>' + s + '</b>' : s;
  }).join('\n');
}

// Fallback when Telegram rejects the HTML: the same text without Markdown markup.
function toPlainText(md) {
  return md.split('\n').map((line) => line
    .replace(/^\s{0,3}#{1,6}\s+/, '')
    .replace(/^(\s*)[-*]\s+/, '$1• ')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '$1 ($2)')
    .replace(/\*\*(?=\S)(.+?)(?<=\S)\*\*/g, '$1')
    .replace(/`([^`\n]+)`/g, '$1')).join('\n');
}

// Splits at paragraph, then line, then character boundaries; each part has at most `limit` characters.
function splitMessage(text, limit) {
  const parts = [];
  let current = '';
  const push = (piece, sep) => {
    if (!current) current = piece;
    else if (current.length + sep.length + piece.length <= limit) current += sep + piece;
    else { parts.push(current); current = piece; }
  };
  for (const para of text.split(/\n{2,}/)) {
    if (para.length <= limit) { push(para, '\n\n'); continue; }
    para.split('\n').forEach((line, index) => {
      const sep = index === 0 ? '\n\n' : '\n';
      if (line.length <= limit) { push(line, sep); return; }
      let piece = '';
      let first = true;
      for (const ch of line) {                 // by code point: never cut a surrogate pair
        if (piece.length + ch.length > limit) { push(piece, first ? sep : ''); piece = ''; first = false; }
        piece += ch;
      }
      push(piece, first ? sep : '');
    });
  }
  if (current) parts.push(current);
  return parts;
}

// The Consultant's result item ({output} from the Agent, or {error} from the failed call) -> Telegram messages.
function formatReply(result) {
  const output = result && typeof result.output === 'string' ? result.output.trim() : '';
  if (!output) return [{ html: escapeHtml(TEXT.unavailable), text: TEXT.unavailable }];
  return splitMessage(output, MAX_CHUNK_CHARS).map((part) => ({ html: toTelegramHtml(part), text: toPlainText(part) }));
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { TEXT, MAX_INPUT_CHARS, MAX_CHUNK_CHARS, sessionIdFor, routeUpdate, escapeHtml, toTelegramHtml,
    toPlainText, splitMessage, formatReply };
}
