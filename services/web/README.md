# Servicio web

Panel de control y ventana del proyector en el navegador, con un servidor
local en Python. El navegador abre la webcam y envía imágenes JPEG para la
detección en vivo; `/projector` dibuja esas mismas detecciones y la pelota.
La interfaz web no usa homografía ni pasos de calibración.

| Carpeta | Contenido |
| --- | --- |
| `backend/` | Servidor HTTP en `127.0.0.1`, estado del juego y contrato HTTP. Ver `backend/README.md`. |
| `ui/` | `index.html` + `app.js` (panel), `projector.html` + `projector.js` (proyector), `ui-core.mjs` (controles) y `live-view.mjs` (contornos y pelota compartidos). |
| `contract/` | `state-matrix.json`: snapshot real de `/api/state` en cada fase y qué acciones acepta el backend allí. `preview-samples.json`: respuestas reales de `/api/preview`, con la paleta que las produjo y si un escaneo de esa misma foto funciona. |
| `tests/` | Pruebas rápidas: `*.test.mjs` (Node), `test_contract_matrix.py` y `test_palette_api.py`. |
| `evals/` | `flows.mjs`: acuerdo entre botones y backend. `live-projection.mjs`: colores y geometría de las dos vistas. `eyedropper.py`: tasa de acierto del gotero. |

## Matriz de estados

`contract/matrix.py` lleva el `GameController` real a once fases (proyector
cerrado, esperando cámara, detección parcial, listo sin esquinas, listo,
listo sin proyector, en juego, en pausa con y sin proyector, llegada y fuera).
En cada fase prueba las cinco acciones del panel sobre un controlador recién construido y guarda
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
`game.width`, `game.height`). Las coordenadas pueden variar sin que falle. El mismo
comando regenera `preview-samples.json`; el test compara su forma, la paleta,
las cuentas por rol y si el escaneo funciona.

## Evaluación del gotero

```sh
./.venv/bin/python -m services.web.evals.eyedropper
```

Corre el JavaScript real del panel (`patchOrigin`, `noteColorHex`,
`weakColor`, `pickVerdict`) sobre fotos de la pared recoloreada y juzga cada
color con el backend real (`palette_set` y `preview_jpeg`). Siete
condiciones: limpia, poca luz, luz cálida, ruido con JPEG fuerte, desenfoque,
y pared beige nítida y desenfocada. 588 clicks: el centro de cada post-it y
50 %, 80 %, 100 %, 110 % y 120 % del camino a cada borde.

Un click acierta si, con los otros roles tomados en su centro, el preview ve
todos los post-its de la muestra (1 inicio, 1 llegada, 1 rebote, 4 esquinas).
Las esquinas sirven para medir el gotero, pero el juego web no las exige. Umbrales:

- adentro del post-it (hasta 80 %): 100 %;
- en el borde (medio cuadrito sobre la pared): 90 % o más;
- ningún fallo silencioso: todo click que falla termina rechazado por el
  servidor, marcado como pálido o explicado por `pickVerdict()`;
- ningún anillo peor que con el promedio simple (`averageHex`, el gotero
  anterior).

Medido el 2026-10-08: adentro 100 %, borde 100 %, afuera 51 % (promedio
simple 35 %; sobre pared beige 47 % contra 12,5 %) y 0 fallos silenciosos.

## Evaluación de flujos

```sh
node services/web/evals/flows.mjs
```

Umbral 1.0. Por fase comprueba que:

- **sound**: todo botón habilitado sea una acción que el backend acepta.
- **complete**: con el proyector abierto, toda acción que el backend acepta
  tenga su botón habilitado.
- **no_camera**: sin cámara, releer y lanzar estén deshabilitados.

Además recorre el camino del operador (detectar, apuntar, lanzar,
reiniciar, continuar una pausa) y verifica que cada fase con el proyector
cerrado se recupere al reabrirlo con `J`. El informe incluye
`disagreements`: la cantidad de diferencias entre panel y backend.

El caso **guidance** prueba cada fase con y sin cámara y con y sin proyector
en todas sus combinaciones. Cuando todos los botones del panel quedan
deshabilitados, el panel tiene que nombrar lo que los destraba: la cámara,
`J` para la proyección o los post-its que faltan. Además, la guía
nunca puede mandar a un botón deshabilitado, y el mensaje del backend no
puede tapar un bloqueo que solo ve el navegador. Informa `dead_states` y
`unguided_states`.

El caso **lights** usa las cinco muestras de `preview-samples.json`:

- pared completa;
- sin inicio;
- con una esquina de más;
- pared recoloreada con los colores por defecto;
- la misma pared con colores tomados de la foto.

En cada muestra, las luces de inicio y llegada tienen que estar encendidas
exactamente cuando la foto permite jugar. Las cuentas de rebotes y esquinas
son informativas y no bloquean el lanzamiento.

## Colores y detección en vivo

Debajo del video, cada rol (inicio, llegada, rebotes, esquinas) tiene:

- una luz;
- un selector de color (`<input type="color">`);
- la cuenta de lo detectado;
- un gotero.

El gotero lee un cuadrito de 11×11 píxeles (`SAMPLE_SIZE`) en el punto
tocado. `framePointFromClick()` descarta las bandas negras de
`object-fit: contain`. `noteColorHex()` se queda con la mediana de la mitad
más saturada del cuadrito: si pisa el borde del post-it, la pared (menos
saturada) queda afuera en vez de lavar el color. Antes de leer, con el
proyector abierto y sin intento en curso, `pickColor()` lo pone en negro
(`projection_black`, confirmación, espera de `scanSettleMs()`) y
después lo restaura. Mientras tanto quedan deshabilitados el gotero,
releer y la búsqueda de ajuste. Si el proyector no confirma el
negro, el panel igual restaura la proyección y explica el motivo.

Ambos caminos (gotero y selector) envían `palette_set`. Hay tres avisos, que
quedan junto a los controles durante 8 segundos:

- el rechazo del servidor (casi gris, muy oscuro o parecido a otro rol);
- `weakColor()`: el color elegido es pálido (saturación menor a 0.35);
- `pickVerdict()`: compara el preview anterior con el primero enviado después
  del cambio y dice qué empeoró ("Con #d2b996 no veo ningún post-it de
  rebotes…", "veo 2 post-its de inicio y van 1…", o que el color nuevo le
  sacó post-its a otro rol).

Cada 250 ms, con una sola foto en vuelo, el panel envía el video a
`POST /api/preview`. Continúa aunque el proyector tenga el foco; pausa
durante una relectura o una búsqueda de cámara. `roleLights()` convierte la respuesta en luces:

- encendida si la cuenta es la necesaria (cualquier cantidad de rebotes);
- ámbar si la cuenta es incorrecta;
- apagada si no ve ninguno.

Sobre la imagen, cada post-it detectado recibe un halo del color de su rol.
`live-view.mjs` dibuja el mismo halo, borde blanco y polígono en `/projector`.
Solo el panel muestra el video. Los dos lienzos usan `containTransform()`:
escalado uniforme y bandas centradas cuando cambia la proporción de pantalla.

`state.live_detection` contiene la última respuesta de `/api/preview`. Caduca
a los 1,5 segundos sin una foto nueva y entonces desaparecen los contornos;
un tablero incompleto también actualiza la proyección. No se recuperan los
contornos de un escaneo anterior. El modo de proyección es `live` desde que
se abre la ventana, y cambiar su tamaño conserva el intento.

La detección válida prepara el juego automáticamente. La física y la pelota
usan coordenadas de cámara; `game.width` y `game.height` identifican el
encuadre del intento. Mientras la pelota corre, las colisiones conservan el
tablero de lanzamiento aunque cambien los contornos en vivo. `reset` usa el
tablero válido más reciente. `scan` sigue disponible como relectura expresa
sin negro previo. Las acciones de calibración ya no forman parte del
contrato web.

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

`PROJECTOR_THEMES` en `ui-core.mjs` define el fondo y la pelota de los temas
`dark` (negro) y `light` (blanco suave `#f3f2ec`). Los halos de los post-its
usan la paleta elegida y bordes blancos en ambos temas, igual que la vista
en vivo. La proyección clara no agrega rebordes negros.

`projectorBackground(theme, forcedBlack)` devuelve `#000000` mientras la
ventana está forzada a negro por el gotero o una búsqueda de cámara,
cualquiera sea el tema. Si no, devuelve el fondo del tema.

El panel guarda la elección en `localStorage` (`postit-wall:projector-theme`)
y la avisa por el `BroadcastChannel` que ya comparte con la ventana del
proyector. También la reenvía cuando la ventana avisa `ready`, así coinciden
aunque el almacenamiento esté bloqueado. La tecla `F` cambia el tema desde
cualquiera de las dos ventanas.

El caso **projector_theme** del eval exige, en los dos temas:

- negro absoluto durante el gotero o la búsqueda de cámara;
- contraste de los colores del juego contra su fondo. Los contornos vivos
  conservan la paleta del panel, independientemente del tema.

## Cámara fija

Al arrancar cada cámara, `setupCameraLock()` hace esto:

1. Lee `getCapabilities()`, después de pedir `ImageCapture.getPhotoCapabilities()`.
   Si no aparece ningún control, reintenta seis veces cada medio segundo.
2. Registra en el log qué controles hay: claves, diagnóstico por control y
   error de `ImageCapture`.
3. Si hay un ajuste guardado con ese nombre de cámara, lo aplica. Si no, corre
   `tuneCamera()`.

**Canal.** Si Chrome no ofrece modo manual para ningún control,
`setupCameraLock()` le pregunta al servidor por `GET /api/camera`, con los
ids USB que Chrome agrega al nombre de la cámara (`usbIdsFromLabel()`). Si la
cámara se puede controlar por USB, pasa a `cameraDriver = "usb"`:

- `applyCamera()` manda los conjuntos de `lockConstraints()` unidos
  (`mergeSets()`) a `POST /api/camera`;
- la búsqueda lee su punto de partida por la misma vía;
- `watchCamera()` revisa cada 20 s si algún modo guardado en manual volvió a
  automático (`driftedModes()`). Si pasó, aplica el ajuste de nuevo y lo
  registra como `reapplied`.

Probado en el navegador con una C920 simulada en el servidor, que parte de la
lectura real:

- Chrome solo informó tamaño y fotogramas, y el panel pasó solo al canal USB;
- buscó, guardó y, al recargar, aplicó el ajuste en una sola llamada;
- con un desvío simulado del balance, el vigilante lo corrigió en unos 9 s.

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
entradas que `controlsFor()`: cámara, proyector, detección de inicio y llegada
o jugar. El panel lo muestra en tres lugares:

- debajo de "Preparar la pared", sin necesidad de hacer scroll;
- en el indicador Cámara → Proyección → Detección → Juego;
- con un anillo en **Abrir proyección** cuando ese es el paso que falta.

`statusText()` hace que un bloqueo del navegador (falta de cámara o de
proyector) tenga prioridad sobre el último mensaje del backend. La guía pide
activar la cámara, abrir la proyección o completar los post-its necesarios.

Esta evaluación encontró el fallo de la pausa: al cerrar el proyector durante
un intento, el backend lo pausa y sigue informando `status: "running"` con
`paused: true`. El panel lo trataba como pelota en movimiento: ocultaba
**Continuar intento** y bloqueaba releer, que el backend sí
acepta. Antes del arreglo: puntuación 0.846 con 2 diferencias; después: 1.0 y
0.

## Pruebas

```sh
node --test services/web/tests/*.test.mjs
node services/web/evals/live-projection.mjs
./.venv/bin/python -m unittest services.web.tests.test_contract_matrix
```

`popup-contract.test.mjs` revisa por texto el cableado que no se puede
ejecutar en Node: que `J` abra el proyector desde el gesto del usuario, el
negro del gotero, el cambio de cámara, el botón de continuar y que
cada `id` que buscan los scripts exista en su HTML.

`live-projection.test.mjs` usa un contexto de dibujo registrable para
comprobar los cuatro colores, el halo, las bandas horizontales y verticales,
las entradas inválidas y que la pelota se transforme una sola vez. El eval
`live-projection.mjs` recorre las muestras de detección del contrato en varios
tamaños; exige una puntuación de 1.0.

La prueba de navegador usa Playwright y Chrome en un perfil aislado, con una
cámara sintética. No abre la webcam física ni cambia los datos guardados del
juego. Si Playwright no está disponible como paquete local, usá la ruta de
un módulo ya instalado:

```sh
POSTIT_PLAYWRIGHT_MODULE=/ruta/a/playwright/index.mjs node tests/live-browser.mjs
```

El script inicia su propio servidor local y escribe capturas e informe en
`/tmp/postit-live-browser/`. Verifica detección antes del lanzamiento, partida
sin calibración, cambios de tamaño, ambos fondos, contornos en movimiento y
desaparición de detecciones viejas.

Al extraer funciones o constantes a un módulo compartido, ejecutá tanto los
tests de importación como esta prueba de navegador. Una constante que quedó
sin importar puede pasar `node --check` y detener el panel recién al cargar;
el test de navegador falla ante `pageerror` y guarda una captura del fallo.
