<?php
// Copia este archivo como config.php y ajústalo. Todo es opcional.
return [
  // Por defecto: SQLite dentro de una carpeta data-XXXX con nombre aleatorio que se
  // crea sola la primera vez. No hace falta tocar nada.
  // 'db_dsn' => 'sqlite:/ruta/fuera/de/la/web/sentinel.sqlite',
  // MySQL/MariaDB del hosting, si lo prefieres:
  // 'db_dsn'  => 'mysql:host=localhost;dbname=sentinel;charset=utf8mb4',
  // 'db_user' => 'usuario',
  // 'db_pass' => 'contraseña',

  // 'data_dir' => '/ruta/fuera/de/la/web',  // si tu hosting lo permite, mejor fuera de la web
  'media_ttl_h' => 24,                  // horas que se guarda un vídeo pedido desde la web
  'media_max_mb_per_device' => 600,     // espacio máximo por comma
  'allow_register' => true,             // false = nadie más puede crear cuenta
];
