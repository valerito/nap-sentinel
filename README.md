# NAP Sentinel

Modo vigilancia para **NotAutopilot** en comma 4 / comma 3X, pensado para cuando el coche está aparcado.

Si alguien golpea, levanta o balancea el coche, sentinel graba las cámaras (frontal, gran angular y habitáculo con infrarrojos) y te avisa por **Telegram** con el vídeo. Todo se gestiona desde un panel web en el propio comma: `http://<IP del comma>:8090`.

<img src="docs/sentinel-web.png" width="300" align="right">

- Se instala **encima de tu NAP actual** con una línea. No hace falta cambiar de rama ni compilar.
- **Pre-grabación** opcional: el vídeo empieza unos segundos **antes** del golpe.
- **Telegram**: aviso con el motivo y la fuerza, vídeo de la gran angular y comandos como `/grabar` o `/estado`.
- **Si eras tú**, no hay aviso: al arrancar el coche durante la grabación, el evento se descarta.
- **Sobrevive a las actualizaciones de NAP.** Mientras conduces no cambia nada.

<br clear="right">

## Índice

- [Instalar, actualizar y desinstalar](#instalar-actualizar-y-desinstalar)
- [Primeros pasos](#primeros-pasos)
- [Modos de vigilancia](#modos-de-vigilancia)
- [Avisos por Telegram](#avisos-por-telegram)
- [Panel web](#panel-web)
- [Qué detecta](#qué-detecta)
- [Ajustes](#ajustes)
- [Energía](#energía)
- [Qué toca en el comma](#qué-toca-en-el-comma)
- [Solución de problemas](#solución-de-problemas)
- [Limitaciones y avisos](#limitaciones-y-avisos)
- [Historial de versiones](#historial-de-versiones)
- [Desarrollo](#desarrollo)

## Instalar, actualizar y desinstalar

Necesitas acceso SSH al comma. En el comma, ve a *Ajustes → Desarrollador → SSH* y añade tu usuario de GitHub. Después, desde tu PC, en la misma red que el comma: `ssh comma@IP_DEL_COMMA`.

**Instalar o actualizar**, siempre con el coche aparcado:

```bash
curl -fsSL https://raw.githubusercontent.com/valerito/nap-sentinel/main/dist/nap-sentinel-install.sh | bash
```

- El instalador pregunta si quieres reiniciar. Hay que reiniciar para que arranque la versión nueva.
  - `--yes`: instala y reinicia sin preguntar.
  - `--no-reboot`: no reinicia.
- Para **actualizar**, se usa el mismo comando. Se conservan tus ajustes, el bot de Telegram y las grabaciones.
- Se puede actualizar con sentinel en marcha.
- La primera línea que imprime indica la versión (`NAP Sentinel 1.x.y`). Si sale una versión antigua, GitHub aún tiene la anterior en caché: espera un par de minutos y repite.

**Alternativa sin internet en el comma**: copia el archivo desde tu PC.

```bash
scp dist/nap-sentinel-install.sh comma@IP_DEL_COMMA:/data/
ssh comma@IP_DEL_COMMA 'bash /data/nap-sentinel-install.sh'
```

**Desinstalar.** NAP queda exactamente como estaba.

```bash
bash /data/sentinel/uninstall.sh            # pregunta si borrar las grabaciones
bash /data/sentinel/uninstall.sh --purge    # borra también las grabaciones
```

## Primeros pasos

1. Tras reiniciar, abre `http://IP_DEL_COMMA:8090` desde el móvil o el PC, en la misma red que el comma (el Wi-Fi de casa o el hotspot del comma).
2. Activa **Sentinel activado**.
3. Ajusta la **sensibilidad** mirando el medidor de vibración en directo.
4. Con el coche apagado, pulsa **Grabar ahora** y comprueba que aparece el evento con su vídeo.
5. Opcional: vincula **Telegram** (ver más abajo).
6. Opcional: pon una **contraseña** a la web en *Avanzado*.

Al apagar el coche, sentinel espera 90 s ("Armando"), para que te dé tiempo a salir y cerrar, y después pasa a **Vigilando**.

## Modos de vigilancia

| | Normal | Pre-grabación |
|---|---|---|
| Qué está encendido mientras vigila | Solo el acelerómetro y el giroscopio | Acelerómetro, cámaras y codificador |
| Consumo aparcado | El comma despierto, del orden de 1–2 W | ≈ +2 W continuos (`camerad` + `encoderd`) |
| Inicio del clip | ~2–3 s **después** del golpe | 5–30 s **antes** del golpe (10 por defecto) |

Se cambia desde la web con el interruptor **Pre-grabación**. Con pre-grabación, el vídeo tiene un botón **⏩ Ir al golpe**.

En pre-grabación, sentinel lee el vídeo que ya codifica el hardware (`encoderd`) y guarda en RAM los últimos N segundos, unos 30–40 MB para 10 s. Siempre empieza en un fotograma clave. Al detectar un evento, escribe ese búfer y sigue grabando en directo. No usa `loggerd`, así que los clips no se suben a comma connect.

Si el comma se calienta demasiado (estado térmico rojo), la pre-grabación se pausa y no se graba.

## Avisos por Telegram

<img src="docs/telegram-web.png" width="300" align="right">

**Vincular**, desde el panel web, sección **Telegram**:

1. En Telegram, abre **@BotFather**, envía `/newbot`, elige un nombre y copia el **token** que te da.
2. Pega el token en la web y pulsa **Guardar**.
3. Pulsa **Vincular mi Telegram**. Se abre tu bot; pulsa **Iniciar**.
   - Queda vinculado solo ese chat.
   - El enlace caduca a los 15 min.
   - Si Telegram está en otro dispositivo, envía al bot el `/start <código>` que muestra la web.
4. Pulsa **Enviar prueba** para comprobarlo.

**Qué recibes:**

- **Aviso de evento**: el motivo (💥 golpe, 📐 inclinación o ↔️ balanceo), la fuerza, la hora y la tensión de la batería.
  - Por defecto espera **30 s** antes de enviarse. Si en ese tiempo arrancas el coche, el evento se descarta y **no recibes nada**.
  - El tiempo se cambia en *Esperar antes de avisar*. Con 0, el aviso es inmediato.
  - La grabación manual (`/grabar` o *Grabar ahora*) avisa al instante.
  - Si arrancas el coche cuando el aviso ya se envió, llega un mensaje de "era el dueño, descartado".
- **Vídeo de la cámara gran angular** en baja calidad (H.264 ≈1 Mbps, ≈7 MB por minuto), como respuesta al aviso, en cuanto termina el clip.
  - Si usas pre-grabación, incluye los segundos anteriores al golpe. La miniatura sale del propio vídeo, en el momento del golpe.
  - Lo codifica el hardware del comma (`stream_encoderd`), que solo funciona mientras se graba, así que no gasta CPU.
  - Si por algún motivo no hay clip de la gran angular, se envía el de la frontal y el pie de foto lo indica.
  - Sin conexión, se reintenta durante 24 h. Opción **Vídeo solo por Wi-Fi** para no gastar datos móviles.
- **Comandos** desde tu chat (el bot ignora cualquier otro chat):

  | Comando | Qué hace |
  |---|---|
  | `/estado` | Estado de sentinel, batería, consumo, temperatura, horas aparcado y último evento |
  | `/grabar` | Graba un clip ahora (con el coche aparcado) |
  | `/ultimo` | Reenvía el vídeo del último evento |
  | `/activar` · `/desactivar` | Activa o desactiva sentinel |

**Si un vídeo no llega**, nunca falla en silencio:
- El motivo llega al chat y se ve en el panel: icono ✈️⚠️ en el evento y *Telegram → Registro de envíos*.
- Puedes reenviar cualquier evento con el botón **✈️ Telegram**.
- Si Telegram rechaza el vídeo, sentinel lo reintenta sin miniatura y, en último caso, lo manda como archivo.

El token del bot se guarda solo en el comma (`/data/sentinel/config.json`). No aparece en la web ni en los registros.

<br clear="right">

## Panel web

`http://IP_DEL_COMMA:8090`, desde cualquier dispositivo en la misma red.

- **Estado**: vigilando, armando, grabando o conduciendo. También muestra la batería de 12 V, el consumo, la temperatura, el espacio libre y, en pre-grabación, los segundos en memoria.
- **Vibración en directo** frente al umbral, para calibrar la sensibilidad.
- **Eventos** con miniatura, motivo, fuerza, duración y estado del envío a Telegram. Para cada evento:
  - Reproductor con pestañas por cámara: Frontal, Habitáculo, Gran angular, Gran angular (ligera) y Frontal HD.
  - Botones ⏩ Ir al golpe, ⬇ Descargar, ✈️ Telegram, 🔒 Bloquear (nunca se borra automáticamente) y Borrar.
  - Si alguna cámara no se pudo convertir, el evento muestra el motivo.
- **Ajustes** y **Telegram**.

Los vídeos HD y de habitáculo son HEVC: se ven en Safari/iOS y en Chrome/Edge con aceleración por hardware. La frontal y la gran angular ligeras (H.264) se ven en cualquier navegador.

## Qué detecta

- **Golpe:** aceleración filtrada paso alto. El umbral depende de la sensibilidad: 1 = 0,30 g · 2 = 0,18 g · 3 = 0,10 g · 4 = 0,06 g · 5 = 0,035 g.
- **Inclinación:** cambio lento del vector de gravedad (gato, grúa). 1,5° por defecto.
- **Balanceo:** giroscopio, con el sesgo del sensor eliminado.
- **Manual:** *Grabar ahora* en la web o `/grabar` en Telegram.

Cada golpe nuevo durante una grabación alarga el clip, hasta 3 minutos. Si arrancas el coche durante la grabación, el clip se descarta porque eras tú. Se puede desactivar.

## Ajustes

Se cambian en la web o en `/data/sentinel/config.json`.

| Clave | Defecto | |
|---|---|---|
| `enabled` | false | Activa sentinel |
| `sensitivity` | 3 | 1–5 |
| `prerecord` / `prerecord_s` | false / 10 | Pre-grabación y segundos previos (5–30) |
| `clip_s` | 45 | Segundos tras el golpe; cada golpe nuevo lo alarga (máx. 180) |
| `arm_delay_s` | 90 | Tiempo para salir y cerrar antes de armarse |
| `tilt_deg` | 1.5 | Umbral de inclinación |
| `record_cabin` / `record_wide` / `record_hd` | true | Qué cámaras grabar en HD (la frontal ligera se graba siempre) |
| `discard_on_drive` | true | Descartar el clip si arrancas durante la grabación |
| `max_storage_gb` | 10 | Espacio máximo; borra primero los eventos antiguos no bloqueados |
| `low_voltage` | 11.8 | Por debajo, el comma se apaga para proteger la batería de 12 V |
| `max_parked_hours` | 0 | Apagar tras X horas aparcado (0 = nunca) |
| `web_password` | "" | Si se define, la web pide contraseña (cualquier usuario) |
| `telegram_alerts` | true | Enviar el aviso de evento |
| `telegram_alert_delay_s` | 30 | Segundos de espera antes de avisar; si arrancas en ese tiempo no se avisa (0 = inmediato) |
| `telegram_video` | true | Enviar el vídeo de la gran angular ligera |
| `telegram_video_wifi_only` | false | Esperar a tener Wi-Fi para enviar el vídeo |

## Energía

- Con sentinel activo, openpilot **no se apaga** a las 30 h ni por su batería virtual.
- Se mantiene un **corte real por tensión**: 11,8 V filtrados durante 45 s. Además, no se graba por debajo de 11,9 V.
- Opcionalmente, puedes apagar el comma tras X horas aparcado.
- Si el comma está demasiado caliente, no graba.

## Qué toca en el comma

| Dónde | Qué |
|---|---|
| `/data/sentinel/` | El programa, tu `config.json` y el registro de Telegram. Está fuera de openpilot, así que no le afectan las actualizaciones de NAP. |
| `/data/openpilot/system/manager/process_config.py` | 12 líneas al final (el "gancho"), dentro de `try/except`: si sentinel falla o no está, openpilot arranca igual que siempre. |
| `/data/continue.sh` | 1 línea que vuelve a poner el gancho en cada arranque. |
| Param `DisablePowerDown` | Activado mientras sentinel está activado. Se restaura al desactivarlo o al desinstalar. |
| `/data/media/0/sentinel/` | Las grabaciones. |

**Actualizaciones de NAP:** al actualizar, el gancho desaparece. Sentinel lo vuelve a añadir de dos formas:
- `continue.sh` lo pone en cada arranque, antes de que openpilot arranque. También lo pone en la actualización pendiente de instalar, así que ya está presente cuando se aplica.
- Sentinel lo comprueba cada 10 minutos.

**Mientras conduces:** el gancho solo amplía las condiciones con el coche apagado, para `sensord`, `camerad`, `encoderd` y `stream_encoderd`. Con el coche encendido, todo funciona exactamente como en NAP sin modificar, y nunca se arranca ningún proceso de control.

## Solución de problemas

```bash
cat /dev/shm/nap_sentinel_status.json                 # estado en vivo de sentineld
cat /data/sentinel/VERSION                            # versión instalada
ls -la /data/media/0/sentinel/<evento>/               # ficheros de un evento
cat /data/media/0/sentinel/<evento>/event.json        # detalles (fotogramas, errores de exportación)
cat /data/media/0/sentinel/<evento>/telegram.json     # estado del envío a Telegram
tail -50 /data/sentinel/telegram.log                  # registro de Telegram
```

| Síntoma | Qué mirar |
|---|---|
| La web no carga | ¿Estás en la misma red? ¿Reiniciaste tras instalar? `grep nap-sentinel /data/openpilot/system/manager/process_config.py` debe mostrar el gancho. |
| Telegram no manda el vídeo | Icono ✈️⚠️ del evento o *Registro de envíos*. `event.json` → `export_errors` indica qué cámara no se pudo convertir. |
| No recibo el aviso | ¿Arrancaste el coche en los primeros 30 s? Entonces se descartó a propósito. Revisa *Esperar antes de avisar*. |
| Demasiados avisos | Baja la sensibilidad o sube *Esperar antes de avisar*. |
| El instalador muestra una versión vieja | Caché de GitHub: espera un par de minutos y repite. |

## Limitaciones y avisos

- Las pruebas reales se han hecho en un comma 4 con NotAutopilot. El README de NAP solo lista el 3X, pero el código de NAP ya incluye la interfaz del comma 4.
- Para acceder a la web desde fuera de casa necesitas una VPN en el comma (p. ej. Tailscale), que no está soportada oficialmente en AGNOS. Telegram funciona desde cualquier sitio.
- Viento fuerte, lluvia intensa o camiones pasando pueden provocar disparos con sensibilidad 4–5.
- Legal (España): grabar la vía pública desde un coche aparcado es videovigilancia (RGPD/AEPD). Graba solo ante eventos y no difundas los vídeos.

## Historial de versiones

| Versión | Cambios |
|---|---|
| 1.1.5 | La miniatura de Telegram sale del propio vídeo enviado (gran angular), y el pie indica si se usó la frontal por falta de gran angular. README actualizado. |
| 1.1.4 | Corrige la conversión a MP4 en el comma (PyAV 13 y `ffmpeg` sin H.264): ya se generan los vídeos ligeros que se mandan a Telegram. Los eventos anteriores se reparan solos. |
| 1.1.3 | El aviso de Telegram espera 30 s (configurable), así que arrancar el coche lo cancela. Los errores de exportación quedan guardados y se muestran. |
| 1.1.2 | Envío a Telegram más robusto (metadatos, miniatura válida, reintentos alternativos); `/ultimo` siempre responde; registro de envíos y botón ✈️ en la web. |
| 1.1.1 | Corrige la actualización con sentinel en marcha. |
| 1.1.0 | Telegram: vinculación desde la web, avisos, vídeo de la gran angular y comandos. |
| 1.0.0 | Primera versión: detección, pre-grabación, panel web e instalador de una línea. |

## Desarrollo

```bash
./build.sh                      # genera dist/nap-sentinel-install.sh (lee VERSION)
PYTHONPATH=.:/ruta/a/openpilot python3 -m pytest nap_sentinel/tests
```

Archivos:
- `nap_sentinel/hook.py`: amplía `should_run` de sensord, camerad, encoderd y stream_encoderd con el coche apagado, y registra los procesos de sentinel.
- `install_hook.py` y `scripts/ensure.sh`: ponen y quitan el gancho.
- `scripts/install.sh.in` y `scripts/uninstall.sh`: instalador y desinstalador.
- `sentineld.py`: máquina de estados, energía, grabación y exportación.
- `detector.py`: detección de golpe, inclinación y balanceo.
- `recorder.py`: búfer circular y escritura de las cámaras desde los mensajes de `encoderd`.
- `exporter.py`: conversión a MP4 sin recodificar (compatible con PyAV 13+).
- `telegram.py`: bot de Telegram (vinculación, avisos, vídeo, comandos, registro).
- `webd.py` y `web/index.html`: panel web.
- `config.py` y `storage.py`: ajustes, estado compartido y eventos.
