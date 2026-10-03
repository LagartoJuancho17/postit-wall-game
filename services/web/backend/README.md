# Backend web local

El navegador es dueño de la webcam (`getUserMedia`) y del lienzo del
proyector. Python recibe fotos JPEG en dos casos: al releer el tablero **entre
intentos** y, para la detección en vivo del panel, unas cuatro veces por
segundo. Nunca abre la cámara, así que macOS usa el permiso de cámara de
Chrome, que ya está habilitado.

El servidor escucha únicamente en `127.0.0.1`. `create_server(port=0)` devuelve
un `ThreadingHTTPServer` sin iniciar; `serve(port=8765)` lo inicia hasta que se
cierre el proceso. Para pruebas sin sockets, `create_server(port=8765,
bind=False)` crea el handler sin llamar a `bind`.

## Contrato HTTP

| Método y ruta | Uso |
| --- | --- |
| `GET /` | Panel de cámara y controles. |
| `GET /projector` | Lienzo emergente del proyector. |
| `GET /api/state` | Estado actual, geometría en píxeles de cámara y proyector. |
| `GET /api/tick` | Avance por reloj monotónico de la física a pasos de 1/120 s. |
| `POST /api/frame` | Bytes `image/jpeg` de una captura oscura; primera lectura o relectura. |
| `POST /api/preview` | Bytes `image/jpeg` del video en vivo; devuelve lo detectado por rol sin cambiar el juego. |
| `POST /api/action` | Objeto JSON con una acción de la tabla siguiente. |

Las escrituras requieren `X-Postit-Client: 1`. Si se envía `Origin`, debe ser
exactamente `http://127.0.0.1:<puerto>`. También se valida `Host` en todas las
rutas para evitar rebinding. El cuerpo HTTP tiene un límite de 8 MB y el JPEG
un límite de 12 megapíxeles, comprobado **antes** de decodificarlo.

| `action` | Datos | Efecto |
| --- | --- | --- |
| `projection_open` | `width`, `height` en píxeles reales del canvas | Abre o redimensiona; al cambiar tamaño invalida la calibración. |
| `projection_close` | ninguno | Oscurece y pausa el intento activo. |
| `camera_changed` | ninguno | Invalida capturas, calibración y tablero tras cambiar de cámara. |
| `projection_black` | ninguno | Prepara una lectura oscura. |
| `calibrate` | ninguno | Empieza la alineación manual de cuatro cruces con los fucsias. |
| `calibration_move` | `x`, `y` | Mueve la cruz activa. |
| `calibration_confirm` | `x`, `y` opcionales | Guarda la cruz; la cuarta calcula la homografía y prepara el tablero. |
| `aim` | `degrees` | Dirección del próximo lanzamiento. |
| `aim_left`, `aim_right` | ninguno | Ajusta la dirección 5°. |
| `launch` | ninguno | Inicia un intento. |
| `reset` | ninguno | Crea una pelota nueva sin releer papeles. |
| `pause`, `resume` | ninguno | Pausa o reanuda el intento. |
| `palette_set` | `role` (`start`, `goal`, `bumper`, `marker`), `hex` (`#rrggbb`) | Cambia el color de ese rol; responde 400 con el motivo si es casi gris, muy oscuro o parecido a otro rol. |
| `palette_reset` | ninguno | Vuelve a los colores por defecto. |
| `camera_preset_save` | `camera` (nombre), `settings`, `score`, `black`, `trials`, `capabilities` | Guarda el ajuste fijo de esa cámara. `settings` solo admite `whiteBalanceMode`, `exposureMode` y `focusMode` en `"manual"`, y `colorTemperature`, `exposureTime`, `focusDistance`, `brightness`, `contrast`, `saturation` y `sharpness` como números finitos. Responde 400 ante cualquier modo automático. |
| `camera_preset_clear` | `camera` | Borra el ajuste de esa cámara. |
| `camera_report` | `camera`, `report` (objeto de hasta 20 KB) | Solo registra en el log lo que el navegador pudo controlar y aplicar; no cambia el juego. |
| `projection_restore` | ninguno | Tras un negro sin tablero nuevo (escaneo fallido o búsqueda de cámara), vuelve a mostrar juego, calibración o negro según corresponda. |

Antes de `/api/frame`, la interfaz envía `projection_black`, espera la
confirmación de que la ventana pintó negro y deja 150 ms para que el proyector
y la webcam se estabilicen. El backend exige que la ventana esté abierta y el
modo sea `black`. La primera captura produce `camera_board`, con coordenadas
de cámara. Tras cuatro puntos manuales, `board` contiene la misma geometría
en píxeles del proyector. Las capturas posteriores aplican esa misma matriz y
preparan una pelota nueva. Si se mueve la webcam o el proyector, hay que
calibrar otra vez. Una captura fallida conserva el tablero anterior.

`GET /api/state` incluye `phase`, `message`, `camera` (dimensiones de la última
captura), `camera_board`, `board`, `palette`, `calibration`, `game` y
`projection`. `palette` es `{"custom": bool, "roles": {rol: {"hex",
"hue_low", "hue_high", "min_saturation", "min_value", "custom"}}}`: el color
de muestra de cada rol y la regla que aplica el detector. El escaneo usa esa
paleta; cambiarla no altera un tablero ya leído hasta el próximo escaneo. Los
colores elegidos se guardan en `runs/palette.json` (local, fuera de git). Al
iniciar se recuperan; si el archivo está dañado, se ignora, se usan los
colores por defecto y se registra `palette_ignored`.

`POST /api/preview` responde `{"width", "height", "roles": {rol: {"count",
"expected", "notes": [polígono, ...]}}, "quality": {...}}` en píxeles de esa
foto. `expected` vale 1 para inicio y llegada, 4 para esquinas y `null` para
los rebotes opcionales. `quality` viene de `services.vision.frame_quality`:

- `ready`: si el escaneo de esa foto funcionaría;
- `clipped` y `dark`: fracción de píxeles quemados y negros;
- `brightness`: brillo mediano;
- `sharpness`: nitidez;
- `note_saturation`: saturación dentro de los post-its.

El panel usa `quality` para elegir el ajuste fijo de cámara. Igual que el
escaneo, usa el mismo detector y la misma paleta, pero no exige cantidades ni
toca el estado del juego.

`camera_presets` en el estado es `{nombre de cámara: {"settings", "score",
"black", "saved_at"}}`, persistido en `runs/camera-presets.json`. Si el
archivo está dañado, se ignora y se registra `camera_presets_ignored`. Cada
polígono se codifica como `{ "points": [{ "x": ..., "y": ... }, ...] }`.
`projection.target` incluye `x`, `y`, `index` (1 a 4) y `label` durante la
calibración. `width` y `height` son los píxeles CSS lógicos de
`window.innerWidth` y `window.innerHeight`; el popup puede multiplicar el
respaldo del canvas por `devicePixelRatio`, siempre que el contexto se escale
por el mismo valor. Las coordenadas de la homografía siguen siendo píxeles
CSS lógicos. La física usa el reloj monotónico del servidor, por
lo que dos clientes consultando `/api/tick` no aceleran la pelota. Un hueco de
más de 250 ms pausa ese intervalo, como hace el modo Pygame.

## Evidencia

`runs/web-events.jsonl` guarda eventos de tamaño del proyector, capturas,
calibración, cambios de color (`palette_changed`, `palette_reset`), ajustes
de cámara (`camera_preset_saved` y `camera_report` con `kind`
`capabilities`, `applied` o `search`) y resultado de intentos. La evaluación sintética comprueba varias cosas:

- error de mapeo menor de 1 px;
- victoria;
- relectura de un rebote movido;
- pérdida de calibración al redimensionar;
- una pared pintada con otros colores (`recolored_wall`). Con los colores por
  defecto se lee mal y el escaneo se rechaza. Tras tomar cada color de la
  foto, la vista previa ve 1/1/1/4, el escaneo funciona y el intento termina
  en victoria.

```sh
./.venv/bin/python -m services.web.backend.eval
```

Las pruebas HTTP usan handlers sin socket ni hardware. La prueba pendiente en
la pared es verificar visualmente los contornos después de colocar webcam y
proyector fijos. Con solo cuatro cruces, la homografía ajusta los puntos
seleccionados pero no estima el error en puntos de control independientes.
