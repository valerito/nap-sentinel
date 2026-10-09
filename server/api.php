<?php
/*
 * NAP Sentinel · acceso remoto (servidor intermedio)
 *
 * Funciona en un hosting compartido con PHP 7.4+ y SQLite o MySQL. No necesita
 * procesos permanentes: el comma consulta este servidor cada pocos segundos
 * (cuando alguien tiene la web abierta) o cada minuto (el resto del tiempo):
 *
 *   comma ──sync──▶ api.php  (sube estado/eventos, recoge órdenes, devuelve resultados)
 *   web   ──cmd───▶ api.php  (encola una orden; el comma la ejecuta contra su panel local)
 *
 * Los vídeos solo se suben cuando un usuario los pide, y se borran del servidor
 * pasadas `media_ttl_h` horas. Los secretos del comma (tokens de Telegram/Tesla,
 * contraseña web) nunca salen del comma: solo se ve lo que el panel local publica.
 */
declare(strict_types=1);

const VERSION = '1.0';
const EVENT_RE = '/^[0-9]{8}-[0-9]{6}(-[0-9]+)?$/';
const MEDIA_NAMES = ['road.mp4', 'fcamera.mp4', 'ecamera.mp4', 'dcamera.mp4', 'wide_lq.mp4', 'thumb.jpg'];
const PAIR_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';

// Same list the comma enforces (nap_sentinel/cloud.py). Anything else is refused on both sides.
const ALLOWED = [
  ['GET',  '#^/api/(status|events|night|update|telegram/log)$#'],
  ['POST', '#^/api/(config|record|reboot|telegram/test|update/check|update/run|tesla/flash|tesla/vehicles|tesla/vehicle)$#'],
  ['POST', '#^/api/events/[0-9]{8}-[0-9]{6}(-[0-9]+)?/(delete|lock|telegram)$#'],
  ['UPLOAD', '#^/media/[0-9]{8}-[0-9]{6}(-[0-9]+)?/(road|fcamera|ecamera|dcamera|wide_lq)\.mp4$#'],
];

function cfg(string $k) {
  static $c = null;
  if ($c === null) {
    $dir = default_data_dir();
    $c = [
      'db_dsn' => 'sqlite:' . $dir . '/sentinel.sqlite',
      'db_user' => null,
      'db_pass' => null,
      'data_dir' => $dir,
      'media_ttl_h' => 24,
      'media_max_mb_per_device' => 600,
      'max_file_mb' => 250,
      'allow_register' => true,
      'poll_active_s' => 2,
      'poll_idle_s' => 30,
    ];
    if (is_file(__DIR__ . '/config.php')) {
      $c = array_merge($c, (array)(require __DIR__ . '/config.php'));
    }
  }
  return $c[$k];
}

// Data folder with an unguessable name, created on first use, so the database and
// cached videos stay private even on servers that ignore .htaccess (nginx).
function default_data_dir(): string {
  $marker = __DIR__ . '/.datadir.php';
  if (is_file($marker)) return (string)(require $marker);
  $dir = __DIR__ . '/data-' . bin2hex(random_bytes(12));
  if (!is_dir($dir)) mkdir($dir, 0770, true);
  file_put_contents("$dir/.htaccess", "Require all denied\n<IfModule !mod_authz_core.c>\n  Deny from all\n</IfModule>\n");
  file_put_contents("$dir/index.html", '');
  file_put_contents($marker, '<?php return ' . var_export($dir, true) . ";\n", LOCK_EX);
  return $dir;
}

// ── helpers ──────────────────────────────────────────────────────────────
function out($data, int $code = 200): void {
  http_response_code($code);
  header('Content-Type: application/json; charset=utf-8');
  header('Cache-Control: no-store');
  echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
  exit;
}
function fail(string $msg, int $code = 400): void { out(['error' => $msg], $code); }
function body(): array {
  $raw = file_get_contents('php://input');
  $j = json_decode($raw ?: '{}', true);
  return is_array($j) ? $j : [];
}
function now(): int { return time(); }
function client_ip(): string { return $_SERVER['REMOTE_ADDR'] ?? '?'; }

function db(): PDO {
  static $pdo = null;
  if ($pdo) return $pdo;
  $dsn = cfg('db_dsn');
  if (strpos($dsn, 'sqlite:') === 0) {
    $dir = dirname(substr($dsn, 7));
    if (!is_dir($dir)) mkdir($dir, 0770, true);
  }
  $pdo = new PDO($dsn, cfg('db_user'), cfg('db_pass'), [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                                                       PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC]);
  if ($pdo->getAttribute(PDO::ATTR_DRIVER_NAME) === 'sqlite') {
    $pdo->exec('PRAGMA journal_mode=WAL');
    $pdo->exec('PRAGMA busy_timeout=5000');
  }
  migrate($pdo);
  return $pdo;
}

function migrate(PDO $pdo): void {
  $mysql = $pdo->getAttribute(PDO::ATTR_DRIVER_NAME) === 'mysql';
  $auto = $mysql ? 'INTEGER PRIMARY KEY AUTO_INCREMENT' : 'INTEGER PRIMARY KEY AUTOINCREMENT';
  $big = $mysql ? 'MEDIUMTEXT' : 'TEXT';
  $tail = $mysql ? ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4' : '';
  $pdo->exec("CREATE TABLE IF NOT EXISTS users (id $auto, email VARCHAR(190) NOT NULL UNIQUE,
    pass_hash VARCHAR(255) NOT NULL, created INTEGER NOT NULL)$tail");
  $pdo->exec("CREATE TABLE IF NOT EXISTS devices (id VARCHAR(32) PRIMARY KEY, secret_hash VARCHAR(64) NOT NULL,
    user_id INTEGER NULL, name VARCHAR(80) NOT NULL DEFAULT '', pair_code VARCHAR(12) NULL, pair_expires INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL, last_seen INTEGER NOT NULL DEFAULT 0, poll_s INTEGER NOT NULL DEFAULT 60, version VARCHAR(20) NOT NULL DEFAULT '',
    viewer_until INTEGER NOT NULL DEFAULT 0, status_json $big, config_json $big, events_json $big, extra_json $big,
    events_sig VARCHAR(64) NOT NULL DEFAULT '')$tail");
  $pdo->exec("CREATE TABLE IF NOT EXISTS commands (id $auto, device_id VARCHAR(32) NOT NULL, user_id INTEGER NOT NULL,
    method VARCHAR(8) NOT NULL, path VARCHAR(200) NOT NULL, body $big, state VARCHAR(10) NOT NULL DEFAULT 'pending',
    result $big, created INTEGER NOT NULL, sent_at INTEGER NOT NULL DEFAULT 0, done_at INTEGER NOT NULL DEFAULT 0)$tail");
  $pdo->exec("CREATE TABLE IF NOT EXISTS files (device_id VARCHAR(32) NOT NULL, eid VARCHAR(32) NOT NULL, name VARCHAR(20) NOT NULL,
    total INTEGER NOT NULL DEFAULT 0, received INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL,
    PRIMARY KEY (device_id, eid, name))$tail");
  $pdo->exec("CREATE TABLE IF NOT EXISTS hits (k VARCHAR(190) NOT NULL, ts INTEGER NOT NULL)$tail");
}

function rate_limit(string $key, int $max, int $window): void {
  $t = now();
  $n = (int)q1('SELECT COUNT(*) AS n FROM hits WHERE k = ? AND ts > ?', [$key, $t - $window])['n'];
  if ($n >= $max) fail('Demasiados intentos. Espera unos minutos.', 429);
  q('INSERT INTO hits (k, ts) VALUES (?, ?)', [$key, $t]);
}
function q(string $sql, array $args = []): PDOStatement { $s = db()->prepare($sql); $s->execute($args); return $s; }
function q1(string $sql, array $args = []): ?array { $r = q($sql, $args)->fetch(); return $r ?: null; }

function media_dir(string $dev, string $eid): string { return cfg('data_dir') . "/media/$dev/$eid"; }

function housekeeping(): void {
  if (mt_rand(1, 50) !== 1) return;
  $t = now();
  $ttl = (int)cfg('media_ttl_h') * 3600;
  foreach (q('SELECT device_id, eid, name FROM files WHERE updated < ? AND name <> ?', [$t - $ttl, 'thumb.jpg'])->fetchAll() as $f) {
    @unlink(media_dir($f['device_id'], $f['eid']) . '/' . $f['name']);
    q('DELETE FROM files WHERE device_id = ? AND eid = ? AND name = ?', [$f['device_id'], $f['eid'], $f['name']]);
  }
  q('DELETE FROM hits WHERE ts < ?', [$t - 86400]);
  q('DELETE FROM commands WHERE created < ?', [$t - 86400]);
  q("UPDATE commands SET state = 'error', result = ? WHERE state = 'sent' AND ((method <> 'UPLOAD' AND sent_at < ?) OR sent_at < ?)",
    [json_encode(['error' => 'el comma no respondió']), $t - 180, $t - 3600]);
}

function allowed(string $method, string $path): bool {
  foreach (ALLOWED as $a) if ($a[0] === $method && preg_match($a[1], $path)) return true;
  return false;
}

// ── sessions (web) ──────────────────────────────────────────────────────
function start_session(): void {
  $https = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') || (($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https');
  session_name('nap_sentinel');
  session_set_cookie_params(['lifetime' => 30 * 86400, 'path' => '/', 'secure' => $https, 'httponly' => true, 'samesite' => 'Lax']);
  ini_set('session.gc_maxlifetime', (string)(30 * 86400));
  session_start();
  if (empty($_SESSION['csrf'])) $_SESSION['csrf'] = bin2hex(random_bytes(16));
}
function require_csrf(): void {
  if ($_SERVER['REQUEST_METHOD'] !== 'POST') fail('método no permitido', 405);
  $t = $_SERVER['HTTP_X_CSRF'] ?? '';
  if (!is_string($t) || !hash_equals($_SESSION['csrf'], $t)) fail('Sesión caducada: recarga la página.', 403);
}
function user_id(): int {
  $u = (int)($_SESSION['uid'] ?? 0);
  if (!$u) fail('Inicia sesión.', 401);
  return $u;
}
function own_device(int $uid, string $id): array {
  $d = q1('SELECT * FROM devices WHERE id = ? AND user_id = ?', [$id, $uid]);
  if (!$d) fail('Ese comma no está en tu cuenta.', 404);
  return $d;
}
function online(array $d): bool { return now() - (int)$d['last_seen'] < max(30, (int)$d['poll_s'] * 2 + 15); }

// ── comma (device) auth ───────────────────────────────────────────────────
function device(): array {
  $id = $_SERVER['HTTP_X_DEVICE_ID'] ?? '';
  $key = $_SERVER['HTTP_X_DEVICE_KEY'] ?? '';
  $d = is_string($id) && $id !== '' ? q1('SELECT * FROM devices WHERE id = ?', [$id]) : null;
  if (!$d || !is_string($key) || !hash_equals($d['secret_hash'], hash('sha256', $key))) fail('comma desconocido', 401);
  return $d;
}
function new_pair_code(): string {
  $s = '';
  for ($i = 0; $i < 6; $i++) $s .= PAIR_ALPHABET[random_int(0, strlen(PAIR_ALPHABET) - 1)];
  return $s;
}
function set_pair_code(string $id): array {
  $code = new_pair_code();
  $exp = now() + 15 * 60;
  q('UPDATE devices SET pair_code = ?, pair_expires = ? WHERE id = ?', [$code, $exp, $id]);
  return ['pair_code' => $code, 'pair_expires_s' => 15 * 60];
}

// ── routes ───────────────────────────────────────────────────────────────
$a = $_GET['a'] ?? '';
try {
  housekeeping();
  switch ($a) {
    // ---------- comma ----------
    case 'd_register': {
      if ($_SERVER['REQUEST_METHOD'] !== 'POST') fail('método no permitido', 405);
      rate_limit('reg_dev:' . client_ip(), 20, 3600);
      $id = bin2hex(random_bytes(8));
      $key = bin2hex(random_bytes(24));
      q('INSERT INTO devices (id, secret_hash, created, version) VALUES (?, ?, ?, ?)',
        [$id, hash('sha256', $key), now(), substr((string)(body()['version'] ?? ''), 0, 20)]);
      out(['device_id' => $id, 'device_key' => $key] + set_pair_code($id));
    }
    case 'd_pair': {
      $d = device();
      out(set_pair_code($d['id']) + ['linked' => (bool)$d['user_id']]);
    }
    case 'd_unlink': {
      $d = device();
      q('UPDATE devices SET user_id = NULL, pair_code = NULL WHERE id = ?', [$d['id']]);
      q("DELETE FROM commands WHERE device_id = ?", [$d['id']]);
      out(['ok' => true]);
    }
    case 'd_sync': {
      $d = device();
      $b = body();
      $t = now();
      foreach ((array)($b['results'] ?? []) as $r) {
        if (!is_array($r) || !isset($r['id'])) continue;
        q("UPDATE commands SET state = ?, result = ?, done_at = ? WHERE id = ? AND device_id = ?",
          [!empty($r['ok']) ? 'done' : 'error', json_encode($r['result'] ?? null, JSON_UNESCAPED_UNICODE), $t, (int)$r['id'], $d['id']]);
      }
      $sets = ['last_seen = ?', 'version = ?'];
      $args = [$t, substr((string)($b['version'] ?? ''), 0, 20)];
      foreach (['status' => 'status_json', 'config' => 'config_json', 'extra' => 'extra_json'] as $k => $col) {
        if (isset($b[$k]) && is_array($b[$k])) { $sets[] = "$col = ?"; $args[] = json_encode($b[$k], JSON_UNESCAPED_UNICODE); }
      }
      if (isset($b['events']) && is_array($b['events'])) {
        $sets[] = 'events_json = ?'; $args[] = json_encode($b['events'], JSON_UNESCAPED_UNICODE);
        $sets[] = 'events_sig = ?'; $args[] = substr((string)($b['events_sig'] ?? ''), 0, 64);
      }
      $active = (int)$d['viewer_until'] > $t;
      $poll = $active ? (int)cfg('poll_active_s') : (int)cfg('poll_idle_s');
      $sets[] = 'poll_s = ?'; $args[] = $poll;
      $args[] = $d['id'];
      q('UPDATE devices SET ' . implode(', ', $sets) . ' WHERE id = ?', $args);

      // an order nobody picked up in 10 min is dropped (a reboot must not happen hours later)
      q("UPDATE commands SET state = 'error', result = ? WHERE device_id = ? AND state = 'pending' AND created < ?",
        [json_encode(['error' => 'caducada: el comma estaba desconectado']), $d['id'], $t - 600]);
      $cmds = [];
      if ($d['user_id']) {
        $rows = q("SELECT id, method, path, body FROM commands WHERE device_id = ? AND state = 'pending' ORDER BY id LIMIT 10", [$d['id']])->fetchAll();
        foreach ($rows as $c) {
          q("UPDATE commands SET state = 'sent', sent_at = ? WHERE id = ?", [$t, $c['id']]);
          $cmds[] = ['id' => (int)$c['id'], 'method' => $c['method'], 'path' => $c['path'], 'body' => json_decode($c['body'] ?: 'null', true)];
        }
        // thumbnails for the event list, a few per sync
        $events = json_decode((string)q1('SELECT events_json FROM devices WHERE id = ?', [$d['id']])['events_json'], true) ?: [];
        $have = [];
        foreach (q("SELECT eid FROM files WHERE device_id = ? AND name = 'thumb.jpg'", [$d['id']])->fetchAll() as $f) $have[$f['eid']] = 1;
        $want = [];
        foreach ($events as $e) {
          $eid = (string)($e['id'] ?? '');
          $files = (array)($e['files'] ?? []);
          if (preg_match(EVENT_RE, $eid) && empty($have[$eid]) && (isset($files['road.mp4']) || isset($files['fcamera.mp4']))) {
            $want[] = ['eid' => $eid, 'name' => 'thumb.jpg'];
            if (count($want) >= 4) break;
          }
        }
        if ($want) $cmds[] = ['id' => 0, 'method' => 'THUMBS', 'path' => '', 'body' => $want];
      }
      $u = $d['user_id'] ? q1('SELECT email FROM users WHERE id = ?', [$d['user_id']]) : null;
      out(['linked' => (bool)$u, 'user' => $u['email'] ?? '', 'commands' => $cmds, 'poll_s' => $cmds ? (int)cfg('poll_active_s') : $poll,
           'want_events' => !isset($b['events']) && ($b['events_sig'] ?? '') !== $d['events_sig']]);
    }
    case 'd_upload': {
      $d = device();
      if (!$d['user_id']) fail('no vinculado', 409);
      $eid = (string)($_GET['eid'] ?? '');
      $name = (string)($_GET['name'] ?? '');
      $offset = (int)($_GET['offset'] ?? -1);
      $total = (int)($_GET['total'] ?? 0);
      if (!preg_match(EVENT_RE, $eid) || !in_array($name, MEDIA_NAMES, true)) fail('archivo no permitido');
      if ($total <= 0 || $total > (int)cfg('max_file_mb') * 1048576) fail('tamaño no permitido', 413);
      $used = (int)(q1('SELECT COALESCE(SUM(total), 0) AS s FROM files WHERE device_id = ?', [$d['id']])['s']);
      $dir = media_dir($d['id'], $eid);
      if (!is_dir($dir)) mkdir($dir, 0770, true);
      $path = "$dir/$name";
      $f = q1('SELECT * FROM files WHERE device_id = ? AND eid = ? AND name = ?', [$d['id'], $eid, $name]);
      if ($offset === 0) {
        if ($used - (int)($f['total'] ?? 0) + $total > (int)cfg('media_max_mb_per_device') * 1048576) fail('sin espacio en el servidor para este comma', 507);
        @unlink($path);
        q('DELETE FROM files WHERE device_id = ? AND eid = ? AND name = ?', [$d['id'], $eid, $name]);
        q('INSERT INTO files (device_id, eid, name, total, received, updated) VALUES (?, ?, ?, ?, 0, ?)', [$d['id'], $eid, $name, $total, now()]);
        $f = ['received' => 0, 'total' => $total];
      }
      if (!$f || (int)$f['received'] !== $offset || (int)$f['total'] !== $total) out(['error' => 'desfase', 'received' => (int)($f['received'] ?? 0)], 409);
      $chunk = file_get_contents('php://input');
      if ($chunk === false || $chunk === '' || $offset + strlen($chunk) > $total) fail('trozo no válido');
      file_put_contents($path, $chunk, FILE_APPEND | LOCK_EX);
      $rec = $offset + strlen($chunk);
      q('UPDATE files SET received = ?, updated = ? WHERE device_id = ? AND eid = ? AND name = ?', [$rec, now(), $d['id'], $eid, $name]);
      out(['received' => $rec, 'complete' => $rec === $total]);
    }

    // ---------- web ----------
    case 'me': {
      start_session();
      $u = !empty($_SESSION['uid']) ? q1('SELECT id, email FROM users WHERE id = ?', [(int)$_SESSION['uid']]) : null;
      out(['user' => $u ? $u['email'] : null, 'csrf' => $_SESSION['csrf'], 'allow_register' => (bool)cfg('allow_register'), 'version' => VERSION]);
    }
    case 'register': {
      start_session(); require_csrf();
      if (!cfg('allow_register')) fail('El registro está cerrado.', 403);
      rate_limit('register:' . client_ip(), 5, 3600);
      $b = body();
      $email = strtolower(trim((string)($b['email'] ?? '')));
      $pw = (string)($b['password'] ?? '');
      if (!filter_var($email, FILTER_VALIDATE_EMAIL) || strlen($email) > 190) fail('Email no válido.');
      if (strlen($pw) < 8) fail('La contraseña debe tener al menos 8 caracteres.');
      if (q1('SELECT id FROM users WHERE email = ?', [$email])) fail('Ese email ya está registrado.', 409);
      q('INSERT INTO users (email, pass_hash, created) VALUES (?, ?, ?)', [$email, password_hash($pw, PASSWORD_DEFAULT), now()]);
      session_regenerate_id(true);
      $_SESSION['uid'] = (int)db()->lastInsertId();
      out(['user' => $email]);
    }
    case 'login': {
      start_session(); require_csrf();
      $b = body();
      $email = strtolower(trim((string)($b['email'] ?? '')));
      rate_limit('login:' . client_ip(), 20, 900);
      rate_limit('login:' . $email, 8, 900);
      $u = q1('SELECT id, pass_hash FROM users WHERE email = ?', [$email]);
      if (!$u || !password_verify((string)($b['password'] ?? ''), $u['pass_hash'])) fail('Email o contraseña incorrectos.', 401);
      session_regenerate_id(true);
      $_SESSION['uid'] = (int)$u['id'];
      out(['user' => $email]);
    }
    case 'logout': {
      start_session(); require_csrf();
      $_SESSION = [];
      session_destroy();
      out(['ok' => true]);
    }
    case 'password': {
      start_session(); require_csrf();
      $uid = user_id();
      $b = body();
      rate_limit('pw:' . $uid, 8, 900);
      $u = q1('SELECT pass_hash FROM users WHERE id = ?', [$uid]);
      if (!password_verify((string)($b['old'] ?? ''), $u['pass_hash'])) fail('La contraseña actual no es correcta.', 401);
      if (strlen((string)($b['new'] ?? '')) < 8) fail('La nueva contraseña debe tener al menos 8 caracteres.');
      q('UPDATE users SET pass_hash = ? WHERE id = ?', [password_hash((string)$b['new'], PASSWORD_DEFAULT), $uid]);
      out(['ok' => true]);
    }
    case 'devices': {
      start_session();
      $uid = user_id();
      $rows = q('SELECT id, name, last_seen, poll_s, version, status_json FROM devices WHERE user_id = ? ORDER BY created', [$uid])->fetchAll();
      out(array_map(function ($d) {
        $st = json_decode((string)$d['status_json'], true) ?: [];
        return ['id' => $d['id'], 'name' => $d['name'] ?: 'Mi comma', 'online' => online($d), 'last_seen' => (int)$d['last_seen'],
                'version' => $d['version'], 'state' => $st['state'] ?? null, 'onroad' => $st['onroad'] ?? null, 'voltage' => $st['voltage'] ?? null];
      }, $rows));
    }
    case 'link': {
      start_session(); require_csrf();
      $uid = user_id();
      rate_limit('link:' . $uid, 10, 3600);
      $b = body();
      $code = strtoupper(preg_replace('/[^A-Za-z0-9]/', '', (string)($b['code'] ?? '')));
      $d = strlen($code) === 6 ? q1('SELECT id, user_id FROM devices WHERE pair_code = ? AND pair_expires > ?', [$code, now()]) : null;
      if (!$d) fail('Código no válido o caducado. Genera uno nuevo en el panel del comma.', 404);
      $name = trim((string)($b['name'] ?? '')) ?: 'Mi comma';
      q('UPDATE devices SET user_id = ?, name = ?, pair_code = NULL, pair_expires = 0, viewer_until = ? WHERE id = ?',
        [$uid, mb_substr($name, 0, 80), now() + 60, $d['id']]);
      out(['id' => $d['id']]);
    }
    case 'rename': {
      start_session(); require_csrf();
      $b = body();
      $d = own_device(user_id(), (string)($b['device'] ?? ''));
      q('UPDATE devices SET name = ? WHERE id = ?', [mb_substr(trim((string)($b['name'] ?? '')) ?: 'Mi comma', 0, 80), $d['id']]);
      out(['ok' => true]);
    }
    case 'unlink': {
      start_session(); require_csrf();
      $b = body();
      $d = own_device(user_id(), (string)($b['device'] ?? ''));
      q('UPDATE devices SET user_id = NULL WHERE id = ?', [$d['id']]);
      q('DELETE FROM commands WHERE device_id = ?', [$d['id']]);
      out(['ok' => true]);
    }
    case 'state': {
      start_session();
      $d = own_device(user_id(), (string)($_GET['device'] ?? ''));
      session_write_close();
      q('UPDATE devices SET viewer_until = ? WHERE id = ?', [now() + 30, $d['id']]);
      $cached = [];
      foreach (q('SELECT eid, name, total, received FROM files WHERE device_id = ?', [$d['id']])->fetchAll() as $f) {
        $cached[$f['eid']][$f['name']] = ['total' => (int)$f['total'], 'received' => (int)$f['received']];
      }
      out(['id' => $d['id'], 'name' => $d['name'] ?: 'Mi comma', 'online' => online($d), 'last_seen' => (int)$d['last_seen'],
           'server_time' => now(), 'poll_s' => (int)$d['poll_s'], 'version' => $d['version'],
           'status' => json_decode((string)$d['status_json'], true), 'config' => json_decode((string)$d['config_json'], true),
           'events' => json_decode((string)$d['events_json'], true) ?: [], 'extra' => json_decode((string)$d['extra_json'], true),
           'cached' => $cached]);
    }
    case 'cmd': {
      start_session(); require_csrf();
      $uid = user_id();
      session_write_close();
      $b = body();
      $d = own_device($uid, (string)($b['device'] ?? ''));
      $method = strtoupper((string)($b['method'] ?? 'GET'));
      $path = (string)($b['path'] ?? '');
      if (!allowed($method, $path)) fail('Orden no permitida.', 403);
      rate_limit('cmd:' . $uid, 120, 60);
      $pending = (int)q1("SELECT COUNT(*) AS n FROM commands WHERE device_id = ? AND state IN ('pending','sent')", [$d['id']])['n'];
      if ($pending > 20) fail('El comma tiene demasiadas órdenes pendientes.', 429);
      q('INSERT INTO commands (device_id, user_id, method, path, body, created) VALUES (?, ?, ?, ?, ?, ?)',
        [$d['id'], $uid, $method, $path, json_encode($b['body'] ?? null, JSON_UNESCAPED_UNICODE), now()]);
      q('UPDATE devices SET viewer_until = ? WHERE id = ?', [now() + 30, $d['id']]);
      out(['id' => (int)db()->lastInsertId(), 'online' => online($d)]);
    }
    case 'cmd_result': {
      start_session();
      $uid = user_id();
      session_write_close();
      $c = q1('SELECT c.state, c.result FROM commands c JOIN devices d ON d.id = c.device_id WHERE c.id = ? AND d.user_id = ?',
              [(int)($_GET['id'] ?? 0), $uid]);
      if (!$c) fail('orden desconocida', 404);
      out(['state' => $c['state'], 'result' => json_decode((string)$c['result'], true)]);
    }
    case 'media': {
      start_session();
      $d = own_device(user_id(), (string)($_GET['device'] ?? ''));
      session_write_close();
      $eid = (string)($_GET['eid'] ?? '');
      $name = (string)($_GET['name'] ?? '');
      if (!preg_match(EVENT_RE, $eid) || !in_array($name, MEDIA_NAMES, true)) fail('archivo no permitido', 404);
      $f = q1('SELECT total, received FROM files WHERE device_id = ? AND eid = ? AND name = ?', [$d['id'], $eid, $name]);
      $path = media_dir($d['id'], $eid) . "/$name";
      if (!$f || (int)$f['received'] !== (int)$f['total'] || !is_file($path)) fail('no está en el servidor', 404);
      send_file($path, $name === 'thumb.jpg' ? 'image/jpeg' : 'video/mp4', !empty($_GET['download']) ? "sentinel-$eid-$name" : null);
    }
    default:
      fail('acción desconocida', 404);
  }
} catch (Throwable $e) {
  error_log('nap-sentinel: ' . $e->getMessage());
  fail('Error del servidor.', 500);
}

function send_file(string $path, string $type, ?string $download): void {
  $size = filesize($path);
  $start = 0; $end = $size - 1;
  header('Accept-Ranges: bytes');
  header("Content-Type: $type");
  header('Cache-Control: private, max-age=3600');
  if ($download) header('Content-Disposition: attachment; filename="' . $download . '"');
  if (isset($_SERVER['HTTP_RANGE']) && preg_match('/bytes=(\d*)-(\d*)/', $_SERVER['HTTP_RANGE'], $m)) {
    if ($m[1] === '' && $m[2] !== '') { $start = max(0, $size - (int)$m[2]); }
    else { $start = (int)$m[1]; if ($m[2] !== '') $end = min((int)$m[2], $size - 1); }
    if ($start > $end || $start >= $size) { http_response_code(416); header("Content-Range: bytes */$size"); exit; }
    http_response_code(206);
    header("Content-Range: bytes $start-$end/$size");
  }
  header('Content-Length: ' . ($end - $start + 1));
  $fh = fopen($path, 'rb');
  fseek($fh, $start);
  $left = $end - $start + 1;
  while ($left > 0 && !feof($fh)) {
    $buf = fread($fh, (int)min(65536, $left));
    echo $buf;
    $left -= strlen($buf);
    flush();
  }
  fclose($fh);
  exit;
}
