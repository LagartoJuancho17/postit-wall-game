# Backend web local

El navegador abre la webcam con `getUserMedia`. Python recibe imágenes JPEG
unas cuatro veces por segundo y devuelve los post-its detectados. El mismo
resultado alimenta la vista en vivo del panel y `/projector`. Python no abre
la cámara y la interfaz web no usa calibración ni homografía.

El servidor escucha únicamente en `127.0.0.1`. `create_server(port=0)` devuelve
un `ThreadingHTTPServer` sin iniciar; `serve(port=8765)` lo inicia hasta que se
cierre el proceso. `create_server(port=8765, bind=False)` permite probar los
handlers sin abrir sockets.

## Contrato HTTP

| Método y ruta | Uso |
| --- | --- |
| `GET /` | Panel de cámara y controles. |
| `GET /projector` | Lienzo del proyector, sin video de cámara. |
| `GET /api/state` | Estado, detección en vivo y geometría en píxeles de cámara. |
| `GET /api/tick` | Avance por reloj monotónico de la física a pasos de 1/120 s. |
| `POST /api/preview` | JPEG del video. Actualiza `live_detection` y prepara el tablero cuando corresponde. |
| `POST /api/frame` | JPEG para releer expresamente y preparar otro intento. No requiere proyector abierto ni negro previo. Durante un intento activo debe pausarse primero. |
| `POST /api/action` | Objeto JSON con una acción de la tabla siguiente. |
| `GET /api/camera?vendor=046d&product=082d` | Controles USB en vocabulario de Chrome: `{"available", "device", "capabilities", "settings"}`, o `{"available": false, "reason"}`. Sin ids usa la única cámara USB conectada. |
| `POST /api/camera` | JSON `{"vendor", "product", "settings"}`: fija la cámara USB en modo `manual`; responde 502 si la cámara rechaza el pedido. |

Las escrituras requieren `X-Postit-Client: 1`. `Host` y, si se envía, `Origin`
deben identificar al servidor local y su puerto. El cuerpo tiene un límite
de 8 MB y el JPEG de 12 megapíxeles, comprobado antes de decodificarlo.
`X-Postit-Camera` admite el nombre de cámara codificado con
`encodeURIComponent`, hasta 200 caracteres.

| `action` | Datos | Efecto |
| --- | --- | --- |
| `projection_open` | `width`, `height` en píxeles CSS | Abre o redimensiona en modo `live`; conserva tablero e intento. |
| `projection_close` | ninguno | Marca la ventana cerrada y pausa el intento. |
| `camera_changed` | ninguno | Borra detección, tablero e intento del encuadre anterior. |
| `projection_black` | ninguno | Negro para tomar colores o buscar ajustes de cámara; requiere pausar un intento activo. |
| `projection_restore` | ninguno | Vuelve al modo `live`. |
| `aim` | `degrees` | Dirección del próximo lanzamiento. |
| `aim_left`, `aim_right` | ninguno | Ajusta la dirección 5°. |
| `launch` | ninguno | Inicia un intento con el tablero listo. |
| `reset` | ninguno | Prepara una pelota con el tablero válido más reciente. |
| `pause`, `resume` | ninguno | Pausa o reanuda el intento. |
| `palette_set` | `role` (`start`, `goal`, `bumper`, `marker`), `hex` (`#rrggbb`) | Cambia el color. Rechaza colores grises, oscuros o demasiado parecidos a otro rol. |
| `palette_reset` | ninguno | Recupera los colores por defecto. |
| `camera_preset_save` | `camera`, `settings`, `score`, `black`, `trials`, `capabilities` | Guarda el ajuste fijo. Los modos de exposición, balance y foco deben ser `manual`; los valores deben ser finitos. |
| `camera_preset_clear` | `camera` | Borra el ajuste de esa cámara. |
| `camera_report` | `camera`, `report` (objeto de hasta 20 KB) | Registra lo que el navegador pudo controlar y aplicar. |

Las antiguas acciones `calibrate`, `calibration_move` y
`calibration_confirm` devuelven 400 como acciones desconocidas. No hay
`calibration` en el estado ni `projection.target`.

## Detección y partida

`POST /api/preview` responde `{"width", "height", "roles", "quality"}`.
Cada rol tiene `count`, `expected` y `notes`. Cada nota contiene un polígono
`{"points": [{"x": ..., "y": ...}, ...]}` en píxeles de esa foto.
`expected` vale 1 para inicio y llegada y `null` para rebotes y esquinas.

`quality` informa `ready`, brillo, fracción de píxeles quemados y oscuros,
nitidez y saturación de los post-its. `ready` exige un inicio y una llegada
únicos y un tablero físicamente válido. Los rebotes son opcionales. Las
esquinas pueden faltar o tener cualquier cantidad; no alinean el proyector.

El resultado más reciente aparece en `state.live_detection` incluso si la
foto no permite jugar. Sin imágenes nuevas durante más de 1,5 segundos,
`live_detection` y el tablero de cámara quedan en `null`. Si la pelota estaba
lista para lanzar, también se descarta. Un intento ya lanzado o terminado
conserva su tablero y su pelota.

Una detección válida prepara automáticamente la pelota si todavía no hay
intento en curso. Durante un intento, los contornos siguen a los papeles y
las colisiones conservan la geometría de lanzamiento. `reset` toma la nueva
distribución. Si no hay una detección válida reciente, no se fabrica otro
tablero con posiciones viejas.

`camera_board` y `board` usan coordenadas de cámara. `game.width` y
`game.height` identifican el encuadre del intento: puede diferir de una cámara
cuyo tamaño cambió después de lanzar. El renderizador aplica una única
transformación proporcional al tamaño actual de la ventana; no estira las
figuras. El respaldo del canvas puede multiplicarse por `devicePixelRatio`,
manteniendo las dimensiones lógicas en píxeles CSS.

La física usa el reloj monotónico del servidor: dos clientes consultando
`/api/tick` no aceleran la pelota. Un hueco mayor que 250 ms no se simula de
golpe. Cerrar la proyección pausa; reabrirla y `resume` continúa el intento.

## Estado persistido y evidencia

`state.palette` describe el color y las reglas HSV por rol. Cambiar la paleta
afecta a la siguiente foto en vivo. Las elecciones se guardan en
`runs/palette.json`. Los ajustes de cámara quedan en
`runs/camera-presets.json` y aparecen en `state.camera_presets`. Un archivo
dañado se ignora y deja un evento en el log.

`runs/web-events.jsonl` registra tamaños de proyector, tableros preparados,
relecturas, cambios de color, ajustes de cámara y resultados de intentos.
Las detecciones incompletas son datos normales del flujo.

```sh
./.venv/bin/python -m services.web.backend.eval
./.venv/bin/python -m tests.eval_web_flow
./.venv/bin/python -m unittest services.web.tests.test_contract_matrix
```

Estas evaluaciones verifican detección que prepara el juego, contornos en
vivo, victoria, rebotes movidos, redimensionado sin perder el intento y una
pared con otros colores. Las pruebas HTTP usan handlers sin socket y sin
hardware. La coincidencia física de cámara, pared y proyector requiere
colocar los equipos para que compartan encuadre.
