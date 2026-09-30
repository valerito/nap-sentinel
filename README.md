# NAP Sentinel

Modo vigilancia para **NotAutopilot** (comma 4 / comma 3X), para cuando el coche está aparcado. Si alguien golpea, levanta o balancea el coche, graba las cámaras frontal, gran angular y de habitáculo (IR). Los clips se ven en el navegador en `http://<IP del comma>:8090`.

<img src="docs/sentinel-web.png" width="320" align="right">

Se instala **encima de tu NAP actual**. No hace falta cambiar de rama ni compilar nada.

## Instalar

Necesitas acceso SSH al comma: en el comma, *Ajustes → Desarrollador → SSH* con tu usuario de GitHub.

**Una línea** (por SSH en el comma):

```bash
curl -fsSL https://raw.githubusercontent.com/valerito/nap-sentinel/main/dist/nap-sentinel-install.sh | bash
```

**Alternativa: copiar el archivo** (desde tu PC, en la misma red que el comma)

```bash
scp dist/nap-sentinel-install.sh comma@IP_DEL_COMMA:/data/
ssh comma@IP_DEL_COMMA 'bash /data/nap-sentinel-install.sh'
```

El instalador pregunta si quieres reiniciar. Opciones: `--yes` instala y reinicia sin preguntar; `--no-reboot` no reinicia. Después del reinicio, abre `http://IP:8090` y activa **Sentinel**.

**Actualizar sentinel:** ejecuta de nuevo el instalador de la versión nueva. Tus ajustes se conservan.

**Desinstalar:**

```bash
bash /data/sentinel/uninstall.sh            # pregunta si borrar las grabaciones
bash /data/sentinel/uninstall.sh --purge    # borra también las grabaciones
```

## Qué toca en el comma

| Dónde | Qué |
|---|---|
| `/data/sentinel/` | El programa y tu `config.json`. Está fuera de openpilot, así que no le afectan las actualizaciones de NAP. |
| `/data/openpilot/system/manager/process_config.py` | 12 líneas al final (el "gancho"), dentro de `try/except`: si sentinel falla o no está, openpilot arranca igual que siempre. |
| `/data/continue.sh` | 1 línea que vuelve a poner el gancho en cada arranque. |
| Param `DisablePowerDown` | Activado mientras Sentinel está activado. Se restaura al desactivarlo o al desinstalar. |
| `/data/media/0/sentinel/` | Las grabaciones. |

**Actualizaciones de NAP:** al actualizar, el gancho desaparece. Sentinel lo vuelve a añadir de dos formas:
- `continue.sh` lo pone en cada arranque, antes de que openpilot arranque. También lo pone en la actualización que queda pendiente de instalar, así que ya está presente cuando se aplica.
- Sentinel lo comprueba cada 10 minutos.

**Mientras conduces:** el gancho solo añade condiciones con el coche apagado. Con el coche encendido, todo funciona exactamente como en NAP sin modificar, y nunca se arranca ningún proceso de control.

## Modos de vigilancia

| | Normal | Pre-grabación |
|---|---|---|
| Qué hay encendido vigilando | Solo el acelerómetro/giroscopio | Acelerómetro + cámaras + codificador |
| Consumo extra aparcado | Mínimo (el comma despierto, del orden de 1–2 W en total) | ≈ +2 W continuos (`camerad` + `encoderd`) |
| Inicio del clip | ~2–3 s **después** del golpe | 5–30 s **antes** del golpe (configurable, 10 por defecto) |

Se cambia desde la web con el interruptor **Pre-grabación**. Con pre-grabación, el vídeo tiene un botón **"Ir al golpe"**.

En pre-grabación, sentinel lee el vídeo que ya codifica `encoderd` y guarda en RAM los últimos N segundos, siempre empezando en un fotograma clave (unos 30–40 MB para 10 s con todas las cámaras). Al detectar un evento, escribe ese búfer y sigue grabando en directo. No usa `loggerd`, así que los clips no se suben a comma connect.

## Qué detecta

- **Golpe:** aceleración filtrada paso alto, con umbral según la sensibilidad (1 = 0,30 g … 5 = 0,035 g).
- **Inclinación:** cambio lento del vector de gravedad (gato, grúa). 1,5° por defecto.
- **Balanceo:** giroscopio, con el sesgo del sensor eliminado.
- **Grabar ahora:** botón en la web.

Si arrancas el coche durante una grabación, el clip se descarta porque eras tú. Se puede desactivar.

La web muestra la vibración en directo frente al umbral. Úsala para calibrar: cierra una puerta, apóyate en el coche y mira qué valores salen.

## Ajustes (web o `/data/sentinel/config.json`)

| Clave | Defecto | |
|---|---|---|
| `enabled` | false | Activa sentinel |
| `sensitivity` | 3 | 1–5 |
| `prerecord` / `prerecord_s` | false / 10 | Pre-grabación y segundos previos (5–30) |
| `clip_s` | 45 | Segundos tras el golpe; cada golpe nuevo lo alarga (máx. 180) |
| `arm_delay_s` | 90 | Tiempo para salir y cerrar antes de armarse |
| `tilt_deg` | 1.5 | Umbral de inclinación |
| `record_cabin` / `record_wide` / `record_hd` | true | Qué cámaras grabar (la frontal ligera se graba siempre) |
| `discard_on_drive` | true | Descartar el clip si arrancas durante la grabación |
| `max_storage_gb` | 10 | Espacio máximo; borra primero los eventos antiguos no bloqueados |
| `low_voltage` | 11.8 | Por debajo, el comma se apaga para proteger la batería de 12 V |
| `max_parked_hours` | 0 | Apagar tras X horas aparcado (0 = nunca) |
| `web_password` | "" | Si se define, la web pide contraseña (cualquier usuario) |

## Energía

- Con sentinel activo, openpilot **no se apaga** a las 30 h ni por su batería virtual.
- Se mantiene un **corte real por tensión** (11,8 V filtrado durante 45 s) y no se graba por debajo de 11,9 V.
- Si el comma está demasiado caliente (estado térmico rojo), no graba y pausa la pre-grabación.

## Limitaciones y avisos

- Todavía no se ha probado en un comma real; se ha verificado con simulación, los tests y un árbol de NAP limpio. Para la primera prueba, con el coche apagado, pulsa **Grabar ahora** en la web.
- Los vídeos HD y de habitáculo son HEVC: se ven en Safari/iOS y en Chrome/Edge con aceleración por hardware. La frontal ligera (H.264) se ve en cualquier navegador.
- Para acceder desde fuera de casa necesitas una VPN en el comma (p. ej. Tailscale), que no está soportada oficialmente en AGNOS.
- Legal (España): grabar la vía pública desde un coche aparcado es videovigilancia (RGPD/AEPD). Graba solo ante eventos y no difundas los vídeos.

## Desarrollo

```bash
./build.sh                      # genera dist/nap-sentinel-install.sh
PYTHONPATH=.:/ruta/a/openpilot python3 -m pytest nap_sentinel/tests
```

Archivos:
- `nap_sentinel/hook.py`: amplía `should_run` de sensord/camerad/encoderd con el coche apagado y registra los procesos de sentinel.
- `install_hook.py` y `scripts/ensure.sh`: ponen y quitan el gancho.
- `sentineld.py`: máquina de estados, energía y grabación.
- `detector.py`: detección de golpe, inclinación y balanceo.
- `recorder.py`: búfer circular y escritura de las cámaras.
- `exporter.py`: conversión a MP4 sin recodificar.
- `webd.py` y `web/index.html`: visor web.
