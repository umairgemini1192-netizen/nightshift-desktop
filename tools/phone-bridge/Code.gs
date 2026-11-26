/**
 * Night-Shift Desktop: Phone Status Bridge Web App (v6-compatible).
 *
 * Receives the redacted 5e deck from Night-Shift Desktop via HTTPS POST,
 * verifies the shared secret in constant time, serialises writes with
 * LockService, enforces monotonic seq ordering, whitelists and length-caps
 * every field, and redraws the Google Doc "NIGHTSHIFT_LIVE_STATUS" with one
 * dark design-token card per project.
 */

const DOC_TITLE = 'NIGHTSHIFT_LIVE_STATUS';
const PROP_SECRET = 'NIGHTSHIFT_BRIDGE_SECRET';
const PROP_DOC_ID = 'NIGHTSHIFT_DOC_ID';
const PROP_CARDS = 'NIGHTSHIFT_CARDS_JSON';

const SILENT_AFTER_MS = 25 * 60 * 1000;
const STALE_CARD_MS = 7 * 24 * 60 * 60 * 1000;

// Design-system dark theme tokens (design/tokens.json)
const TOKENS = {
  bgCanvas: '#05070D',
  bgAbyss: '#0A0F1C',
  surfaceCard: '#101729',
  borderSubtle: '#1B2440',
  slateMuted: '#3A4668',
  textPrimary: '#F5F8FF',
  textSecondary: '#D6DDF2',
  textMuted: '#8F9BBF',
  cyan: '#00D4FF',
  green: '#34F5A0',
  amber: '#FFC145',
  red: '#FF5C7A',
};

const ALLOWED_STATES = {
  working: true,
  attention: true,
  good: true,
  stopped: true,
};

function doGet() {
  return jsonOut_({ ok: true, service: 'nightshift-phone-bridge', doc: DOC_TITLE });
}

function doPost(e) {
  let payload;
  try {
    const raw = (e && e.postData && e.postData.contents) ? e.postData.contents : '{}';
    payload = JSON.parse(raw);
  } catch (_err) {
    return jsonOut_({ ok: false, error: 'invalid_json' });
  }

  const props = PropertiesService.getScriptProperties();
  const expectedSecret = props.getProperty(PROP_SECRET) || '';
  const providedSecret = typeof payload.secret === 'string' ? payload.secret : '';

  if (!expectedSecret || !timingSafeEqual_(providedSecret, expectedSecret)) {
    return jsonOut_({ ok: false, error: 'unauthorized' });
  }

  if (payload.kind !== 'nightshift_deck' || !payload.deck || typeof payload.deck !== 'object') {
    return jsonOut_({ ok: false, error: 'invalid_payload' });
  }

  const seq = Number(payload.seq);
  if (!Number.isFinite(seq) || seq <= 0) {
    return jsonOut_({ ok: false, error: 'invalid_seq' });
  }

  const deck = sanitizeDeck_(payload.deck);
  if (!deck.project) {
    return jsonOut_({ ok: false, error: 'missing_project' });
  }

  const lock = LockService.getScriptLock();
  try {
    lock.waitLock(15000);
  } catch (_err) {
    return jsonOut_({ ok: false, error: 'busy' });
  }
  try {
    const nowMs = Date.now();
    const cards = loadCards_(props, nowMs);
    const existing = cards[deck.project];

    if (existing && Number(existing.seq) >= seq) {
      return jsonOut_({ ok: true, dropped: 'stale_seq' });
    }

    cards[deck.project] = {
      seq: seq,
      updatedAtMs: nowMs,
      deck: deck,
    };

    pruneOldCards_(cards, nowMs);
    props.setProperty(PROP_CARDS, JSON.stringify(cards));
    redrawDoc_(props, cards, nowMs);

    return jsonOut_({ ok: true, project: deck.project, seq: seq });
  } finally {
    lock.releaseLock();
  }
}

/**
 * Optional time-driven trigger (every 10 min) to mark silent projects
 * even when the PC has gone offline or asleep.
 */
function refreshSilentCards() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    const props = PropertiesService.getScriptProperties();
    const nowMs = Date.now();
    const cards = loadCards_(props, nowMs);
    pruneOldCards_(cards, nowMs);
    props.setProperty(PROP_CARDS, JSON.stringify(cards));
    redrawDoc_(props, cards, nowMs);
  } finally {
    lock.releaseLock();
  }
}

function timingSafeEqual_(a, b) {
  const strA = String(a || '');
  const strB = String(b || '');
  const maxLen = Math.max(strA.length, strB.length);
  let diff = strA.length ^ strB.length;
  for (let i = 0; i < maxLen; i++) {
    const ca = i < strA.length ? strA.charCodeAt(i) : 0;
    const cb = i < strB.length ? strB.charCodeAt(i) : 0;
    diff |= (ca ^ cb);
  }
  return diff === 0;
}

function capStr_(val, maxLen) {
  const s = String(val == null ? '' : val)
    .replace(/[\r\n\t]+/g, ' ')
    .replace(/[\x00-\x1F\x7F]/g, '')
    .trim();
  return s.length > maxLen ? s.slice(0, maxLen) : s;
}

function capList_(arr, maxItems, maxItemLen) {
  if (!Array.isArray(arr)) return [];
  const out = [];
  for (let i = 0; i < arr.length && out.length < maxItems; i++) {
    const item = capStr_(arr[i], maxItemLen);
    if (item) out.push(item);
  }
  return out;
}

function sanitizeDeck_(raw) {
  const state = ALLOWED_STATES[raw.state] ? raw.state : 'stopped';
  const nowObj = raw.now && typeof raw.now === 'object' ? raw.now : {};
  const phasesRaw = Array.isArray(raw.phases) ? raw.phases : [];
  const phases = [];
  for (let i = 0; i < phasesRaw.length && phases.length < 20; i++) {
    const ph = phasesRaw[i];
    if (ph && typeof ph === 'object') {
      phases.push({
        title: capStr_(ph.title, 60),
        state: capStr_(ph.state, 32),
      });
    }
  }

  return {
    project: capStr_(raw.project, 60),
    state: state,
    headline: capStr_(raw.headline, 140),
    now: {
      phase: capStr_(nowObj.phase, 80),
      step: capStr_(nowObj.step, 40),
      engine: capStr_(nowObj.engine, 40),
      eta: capStr_(nowObj.eta, 40),
    },
    needs_you: capList_(raw.needs_you, 3, 120),
    done_tonight: capList_(raw.done_tonight, 12, 100),
    next: capList_(raw.next, 3, 100),
    phases: phases,
  };
}

function loadCards_(props, nowMs) {
  try {
    const raw = props.getProperty(PROP_CARDS);
    const parsed = raw ? JSON.parse(raw) : {};
    if (parsed && typeof parsed === 'object') {
      pruneOldCards_(parsed, nowMs);
      return parsed;
    }
  } catch (_err) {
    // Reset corrupted state safely
  }
  return {};
}

function pruneOldCards_(cards, nowMs) {
  Object.keys(cards).forEach(function (key) {
    const entry = cards[key];
    if (!entry || !entry.updatedAtMs || (nowMs - entry.updatedAtMs) > STALE_CARD_MS) {
      delete cards[key];
    }
  });
}

function getOrCreateDoc_(props) {
  const savedId = props.getProperty(PROP_DOC_ID);
  if (savedId) {
    try {
      return DocumentApp.openById(savedId);
    } catch (_err) {
      // Fall through to search/create
    }
  }
  const files = DriveApp.getFilesByName(DOC_TITLE);
  if (files.hasNext()) {
    const doc = DocumentApp.openById(files.next().getId());
    props.setProperty(PROP_DOC_ID, doc.getId());
    return doc;
  }
  const created = DocumentApp.create(DOC_TITLE);
  props.setProperty(PROP_DOC_ID, created.getId());
  return created;
}

function redrawDoc_(props, cards, nowMs) {
  const doc = getOrCreateDoc_(props);
  const body = doc.getBody();
  body.clear();
  body.setBackgroundColor(TOKENS.bgCanvas);
  body.setMarginTop(24);
  body.setMarginBottom(24);
  body.setMarginLeft(24);
  body.setMarginRight(24);

  const title = body.appendParagraph('NIGHT-SHIFT LIVE STATUS');
  title.setHeading(DocumentApp.ParagraphHeading.HEADING2);
  title.editAsText().setForegroundColor(TOKENS.textMuted).setFontSize(11).setBold(true);

  const list = Object.keys(cards).map(function (k) { return cards[k]; });
  const order = { working: 0, attention: 1, good: 2, stopped: 3 };
  list.sort(function (a, b) {
    const oa = order[a.deck.state] != null ? order[a.deck.state] : 4;
    const ob = order[b.deck.state] != null ? order[b.deck.state] : 4;
    if (oa !== ob) return oa - ob;
    return b.updatedAtMs - a.updatedAtMs;
  });

  if (list.length === 0) {
    const empty = body.appendParagraph('No active Night-Shift projects yet.');
    empty.editAsText().setForegroundColor(TOKENS.textSecondary).setFontSize(12);
    doc.saveAndClose();
    return;
  }

  list.forEach(function (entry) {
    renderProjectCard_(body, entry, nowMs);
  });

  doc.saveAndClose();
}

function renderProjectCard_(body, entry, nowMs) {
  const deck = entry.deck;
  const ageMs = Math.max(0, nowMs - entry.updatedAtMs);
  const isSilent = deck.state === 'working' && ageMs > SILENT_AFTER_MS;

  let statusWord = 'STOPPED';
  let statusColor = TOKENS.amber;
  if (isSilent) {
    statusWord = 'SILENT';
    statusColor = TOKENS.amber;
  } else if (deck.state === 'working') {
    statusWord = 'WORKING';
    statusColor = TOKENS.cyan;
  } else if (deck.state === 'attention') {
    statusWord = 'NEEDS YOU';
    statusColor = TOKENS.red;
  } else if (deck.state === 'good') {
    statusWord = 'ALL GOOD';
    statusColor = TOKENS.green;
  }

  const table = body.appendTable([['']]);
  table.setBorderColor(TOKENS.borderSubtle);
  table.setBorderWidth(1);

  const cell = table.getCell(0, 0);
  cell.setBackgroundColor(TOKENS.surfaceCard);
  cell.setPaddingTop(14);
  cell.setPaddingBottom(14);
  cell.setPaddingLeft(16);
  cell.setPaddingRight(16);

  const pStatus = cell.getChild(0).asParagraph();
  pStatus.setText(statusWord + '   ' + deck.project);
  pStatus.editAsText()
    .setFontSize(14)
    .setBold(true)
    .setForegroundColor(0, statusWord.length - 1, statusColor)
    .setForegroundColor(statusWord.length, pStatus.getText().length - 1, TOKENS.textPrimary);

  const pHeadline = cell.appendParagraph(deck.headline || 'Status updated');
  pHeadline.editAsText().setForegroundColor(TOKENS.textSecondary).setFontSize(11);

  if (isSilent) {
    const pWarn = cell.appendParagraph('No update for 25+ min - PC may be off, asleep, or offline.');
    pWarn.editAsText().setForegroundColor(TOKENS.amber).setFontSize(10).setBold(true);
  }

  // Phase strip (wrap into rows of 6 when a plan has more than 8 phases)
  if (deck.phases && deck.phases.length > 0) {
    const chunkSize = deck.phases.length > 8 ? 6 : deck.phases.length;
    const rows = [];
    for (let r = 0; r < deck.phases.length; r += chunkSize) {
      const slice = deck.phases.slice(r, r + chunkSize);
      const rowTitles = slice.map(function (ph) { return ph.title || 'Phase'; });
      while (rowTitles.length < chunkSize) rowTitles.push('');
      rows.push(rowTitles);
    }
    const strip = cell.appendTable(rows);
    strip.setBorderColor(TOKENS.bgCanvas);
    strip.setBorderWidth(1);
    for (let idx = 0; idx < rows.length * chunkSize; idx++) {
      const rIdx = Math.floor(idx / chunkSize);
      const cIdx = idx % chunkSize;
      const sc = strip.getCell(rIdx, cIdx);
      sc.setPaddingTop(4);
      sc.setPaddingBottom(4);
      sc.setPaddingLeft(6);
      sc.setPaddingRight(6);
      if (idx < deck.phases.length) {
        const ph = deck.phases[idx];
        sc.setBackgroundColor(phaseColor_(ph.state));
        sc.getChild(0).asParagraph().editAsText()
          .setForegroundColor(TOKENS.bgCanvas)
          .setFontSize(8)
          .setBold(true);
      } else {
        sc.setBackgroundColor(TOKENS.surfaceCard);
      }
    }
  }

  const nowParts = [deck.now.phase, deck.now.step, deck.now.engine, deck.now.eta].filter(Boolean);
  appendSection_(cell, 'NOW', nowParts.length ? nowParts.join(' · ') : 'Idle');
  if (deck.needs_you.length > 0) {
    appendSection_(cell, 'NEEDS YOU', deck.needs_you.join(' | '), TOKENS.red);
  }
  appendSection_(cell, 'DONE THIS RUN', deck.done_tonight.length ? deck.done_tonight.join(', ') : 'None yet');
  appendSection_(cell, 'NEXT', deck.next.length ? deck.next.join(', ') : 'None queued');

  const minsAgo = Math.floor(ageMs / 60000);
  const heardText = 'Last heard: ' + (minsAgo <= 0 ? 'just now' : minsAgo + ' min ago') +
    ' (' + Utilities.formatDate(new Date(entry.updatedAtMs), Session.getScriptTimeZone(), 'HH:mm') + ')';
  const pHeard = cell.appendParagraph(heardText);
  pHeard.editAsText().setForegroundColor(TOKENS.textMuted).setFontSize(9);

  body.appendParagraph('');
}

function appendSection_(cell, label, text, color) {
  const p = cell.appendParagraph(label + ': ' + text);
  p.editAsText()
    .setFontSize(10)
    .setForegroundColor(0, label.length, TOKENS.textMuted)
    .setBold(0, label.length, true)
    .setForegroundColor(label.length + 1, p.getText().length - 1, color || TOKENS.textSecondary);
}

function phaseColor_(state) {
  if (state === 'merged' || state === 'completed' || state === 'done') return TOKENS.green;
  if (
    state === 'working' ||
    state === 'running' ||
    state === 'implementing' ||
    state === 'watching_ci' ||
    state === 'merging' ||
    state === 'preparing_worktree' ||
    state === 'verifying_build' ||
    state === 'releasing'
  ) {
    return TOKENS.cyan;
  }
  if (state === 'parked' || state === 'failed') return TOKENS.red;
  return TOKENS.slateMuted;
}

function jsonOut_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
