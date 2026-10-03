# Servicio web

Panel de control y ventana del proyector en el navegador, con un servidor
local en Python. El navegador abre la webcam y dibuja la proyección; Python
recibe una foto JPEG solo al escanear.

| Carpeta | Contenido |
| --- | --- |
| `backend/` | Servidor HTTP en `127.0.0.1`, estado del juego y contrato HTTP. Ver `backend/README.md`. |
| `ui/` | `index.html` + `app.js` (panel), `projector.html` + `projector.js` (ventana del proyector) y `ui-core.mjs`, la lógica pura que ambos comparten. |
| `contract/` | `state-matrix.json`: snapshot real de `/api/state` en cada fase y qué acciones acepta el backend allí. `preview-samples.json`: respuestas reales de `/api/preview`, con la paleta que las produjo y si un escaneo de esa misma foto funciona. |
| `tests/` | Pruebas rápidas: `*.test.mjs` (Node), `test_contract_matrix.py` y `test_palette_api.py`. |
| `evals/` | `flows.mjs`: acuerdo entre los botones del panel y el backend. |

## Matriz de estados

`contract/matrix.py` lleva el `GameController` real a once fases (proyector
cerrado, esperando cámara, escaneado, calibrando, listo, en juego, en pausa
con y sin proyector, llegada, fuera, listo sin proyector). En cada fase prueba
las seis acciones del panel sobre un controlador recién construido y guarda
si el backend las acepta. El navegador decide sus botones solo con el
snapshot, así que las pruebas de JavaScript usan estos snapshots reales en
lugar de estados inventados.

```sh
./.venv/bin/python -m services.web.contract.matrix   # regenerar
```

`tests/test_contract_matrix.py` vuelve a construir la matriz y falla si la
guardada ya no coincide con el backend: aceptación de acciones, claves y tipos
del snapshot, y los valores de los que depende el panel (`phase`,
`game.status`, `game.paused`, `projection.open`, `projection.mode`,
`calibration.status`). Las coordenadas pueden variar sin que falle. El mismo
comando regenera `preview-samples.json`; el test compara su forma, la paleta,
las cuentas por rol y si el escaneo funciona.

## Evaluación de flujos

```sh
node services/web/evals/flows.mjs
```

Umbral 1.0. Por fase comprueba que:

- **sound**: todo botón habilitado sea una acción que el backend acepta.
- **complete**: con el proyector abierto, toda acción que el backend acepta
  tenga su botón habilitado.
- **no_camera**: sin cámara, escanear, calibrar y lanzar estén deshabilitados.

Además recorre el camino del operador (escanear, calibrar, apuntar, lanzar,
reiniciar, continuar una pausa) y verifica que cada fase con el proyector
cerrado se recupere al reabrirlo con `J`. El informe incluye
`disagreements`: la cantidad de diferencias entre panel y backend.

El caso **guidance** prueba cada fase con y sin cámara y con y sin proyector
(38 combinaciones). En las 25 donde todos los botones del panel quedan
deshabilitados, el panel tiene que nombrar lo que los destraba: la cámara,
`J` para la proyección o la cruz en la ventana del proyector. Además, la guía
nunca puede mandar a un botón deshabilitado, y el mensaje del backend no
puede tapar un bloqueo que solo ve el navegador. Informa `dead_states` y
`unguided_states`.

El caso **lights** usa las cinco muestras de `preview-samples.json`:

- pared completa;
- sin inicio;
- con una esquina de más;
- pared recoloreada con los colores por defecto;
- la misma pared con colores tomados de la foto.

En cada muestra, las luces de inicio, llegada y esquinas tienen que estar
todas encendidas exactamente cuando el escaneo de esa foto funciona. La
muestra con una esquina de más es la que exige la luz ámbar: sin ella, el
panel diría "listo" y el escaneo fallaría.

## Colores y detección en vivo

Debajo del video, cada rol (inicio, llegada, rebotes, esquinas) tiene:

- una luz;
- un selector de color (`<input type="color">`);
- la cuenta de lo detectado;
- un gotero.

El gotero promedia 7×7 píxeles del cuadro de video en el punto tocado.
`framePointFromClick()` descarta las bandas negras de `object-fit: contain`.
Ambos caminos envían `palette_set`, y un rechazo se muestra junto a los
controles durante 8 segundos.

Cada 250 ms, con una sola foto en vuelo, el panel envía el video a
`POST /api/preview`. Pausa ese envío durante el escaneo y con la pestaña
oculta. `roleLights()` convierte la respuesta en luces:

- encendida si la cuenta es la necesaria (cualquier cantidad de rebotes);
- ámbar si la cuenta es incorrecta;
- apagada si no ve ninguno.

Sobre la imagen, cada post-it detectado recibe un halo del color de su rol.

Si el servidor no tiene esta función (un `web.py` iniciado antes de este
cambio), `/api/preview` responde 404 y el estado no trae `palette`. En ese
caso el panel desactiva los selectores y muestra "Reiniciá web.py…"; el resto
sigue funcionando.

El caso **camera** usa `contract/camera-trials.json`. Ahí hay cinco salas
simuladas: neutra, lámparas cálidas de 3000 K, luz de día de 6000 K, poca luz
y mucha luz. `camera_trials.py` genera, para cada exposición y balance de la
grilla de búsqueda, la foto que tomaría una cámara fija en esa sala y la
puntúa con el backend real. El eval corre el elegidor del panel en cada sala y
exige:

- que el escaneo funcione;
- menos de 2 % de píxeles quemados;
- balance a 1000 K o menos de la luz real;
- exposición entre la mitad y el doble de la ideal.

Un test comprueba que la grilla de `ui-core.mjs` para una C920 sea idéntica a
la del simulador. `verify.sh` regenera las pruebas con `--check`: las cuentas
y `ready` tienen que coincidir exactamente y las medidas dentro de una
tolerancia.

## Fondo del proyector

`PROJECTOR_THEMES` en `ui-core.mjs` define el fondo y cada color de trazo de
los temas `dark` (los colores originales) y `light`.

`projectorBackground(theme, forcedBlack)` devuelve `#000000` mientras la
ventana está forzada a negro por un escaneo o una búsqueda de cámara,
cualquiera sea el tema. Si no, devuelve el fondo del tema.

El panel guarda la elección en `localStorage` (`postit-wall:projector-theme`)
y la avisa por el `BroadcastChannel` que ya comparte con la ventana del
proyector. También la reenvía cuando la ventana avisa `ready`, así coinciden
aunque el almacenamiento esté bloqueado. La tecla `F` cambia el tema desde
cualquiera de las dos ventanas.

El caso **projector_theme** del eval exige, en los dos temas:

- negro absoluto al escanear;
- contraste 4:1 o más de cada trazo contra su fondo (2.5:1 para la cuadrícula
  de guía).

## Cámara fija

Al arrancar cada cámara, `setupCameraLock()` hace esto:

1. Lee `getCapabilities()`, después de pedir `ImageCapture.getPhotoCapabilities()`.
   Si no aparece ningún control, reintenta seis veces cada medio segundo.
2. Registra en el log qué controles hay: claves, diagnóstico por control y
   error de `ImageCapture`.
3. Si hay un ajuste guardado con ese nombre de cámara, lo aplica. Si no, corre
   `tuneCamera()`.

`cameraSupport()` separa dos cosas por control:

- **`lock`**: existe un modo `manual`. Alcanza para apagar el automático y
  congelar el valor actual.
- **`search`**: además hay un rango de valores, así que se pueden probar y
  fijar valores.

Antes se exigían las dos para todo, y una cámara que solo informaba los modos
quedaba en automático con el aviso "no deja fijar". Ese caso está en los tests
como regresión.

`lockConstraints()` arma un conjunto `advanced` por control, siempre en
`manual`. Un conjunto que la cámara no puede cumplir se descarta entero, así
que separarlos evita que un control no soportado anule a los demás.
`fixedPart()` guarda solo lo que `getSettings()` confirma en `manual` e
informa el resto como "sin fijar".

La búsqueda tiene tres etapas:

1. **Grilla**: exposición × balance (`searchPlan()`), con el foco fijo donde
   estaba.
2. **Barrido de foco**: con el mejor punto de la grilla elige la imagen más
   nítida (`sharpestTrial()`).
3. **Refinamiento**: el mejor punto más sus vecinos (`refinePlan()`, ±25 % de
   exposición y ±500 K), todo con el foco nuevo.

Cada prueba espera `settleMs()` (tres cuadros de exposición más 250 ms) y
puntúa con `presetScore()`. Durante la búsqueda se pausan la vista previa
normal y el escaneo, y se bloquea el cambio de cámara. Si el proyector está
listo, se pone en negro; al terminar, `projection_restore` y `resume`
devuelven la imagen.

Probado en el navegador con una cámara simulada que respeta
`applyConstraints()`, con luz de 3000 K, exposición ideal de 30 ms y nitidez
en 160:

- La búsqueda encontró `3500 K · 20 ms · foco 165` (165 es la posición del
  barrido más cercana a 160) en 32 pruebas, todo en manual.
- Al recargar, lo aplicó en una sola llamada sin volver a buscar.
- Con el proyector abierto, lo puso en negro, guardó `black: true` y le
  devolvió la imagen.

## Guía del siguiente paso

`setupGuide()` en `ui-core.mjs` decide el siguiente paso con las mismas
entradas que `controlsFor()`: cámara, proyector, escanear, calibrar, alinear
la cruz o jugar. El panel lo muestra en tres lugares:

- debajo de "Preparar la pared", sin necesidad de hacer scroll;
- en el indicador Cámara → Proyección → Escaneo → Juego;
- con un anillo en **Abrir proyección** cuando ese es el paso que falta.

`statusText()` hace que un bloqueo del navegador (falta de cámara o de
proyector) tenga prioridad sobre el último mensaje del backend. Antes, con la
cámara en vivo y el proyector cerrado, todo quedaba deshabilitado y el único
mensaje decía "Cambió la cámara. Escaneá y calibrá otra vez.", que mandaba a
un botón deshabilitado.

Esta evaluación encontró el fallo de la pausa: al cerrar el proyector durante
un intento, el backend lo pausa y sigue informando `status: "running"` con
`paused: true`. El panel lo trataba como pelota en movimiento: ocultaba
**Continuar intento** y bloqueaba escanear y calibrar, que el backend sí
acepta. Antes del arreglo: puntuación 0.846 con 2 diferencias; después: 1.0 y
0.

## Pruebas

```sh
node --test services/web/tests/*.test.mjs
./.venv/bin/python -m unittest services.web.tests.test_contract_matrix
```

`popup-contract.test.mjs` revisa por texto el cableado que no se puede
ejecutar en Node: que `J` abra el proyector desde el gesto del usuario, el
negro antes de capturar, el cambio de cámara, el botón de continuar y que
cada `id` que buscan los scripts exista en su HTML.
