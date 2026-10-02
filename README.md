# Juego de pared con post-its

La webcam lee un tablero físico y el proyector dibuja una pelota encima. Hay
un post-it de inicio, uno de llegada, rebotes opcionales y **cuatro** de
esquina que marcan el campo. Por defecto, el inicio es amarillo, la llegada
naranja, los rebotes verdes y las esquinas fucsia o violetas. Desde la vista
de cámara podés elegir otros colores. Esta versión relee los post-its **entre
intentos**. La interfaz web muestra la cámara y los controles en la pantalla
principal; `J` abre otra ventana para proyectar sobre la pared.

## Iniciar la interfaz web

Con Python 3.12 y `uv` instalados, ejecutá desde esta carpeta:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
./.venv/bin/python web.py
```

Se abrirá Chrome en `http://127.0.0.1:8765/`. Elegí la Logitech C920 en el
selector de cámaras y permití el acceso a la cámara para esa página si Chrome
lo solicita. El navegador muestra la imagen en la pantalla principal. El
servidor Python nunca abre la cámara, así que no necesita permiso de cámara
de macOS. Recibe fotos JPEG del navegador en dos casos: al escanear y, unas
cuatro veces por segundo, para la detección en vivo.

Pulsá `J` o el botón **Abrir proyector**. Mové la nueva ventana a la pantalla
del proyector y activá su pantalla completa desde el botón de esa ventana.
Mantené esa pantalla en su tamaño definitivo antes de calibrar: un cambio de
tamaño invalida la alineación. Los controles de la partida permanecen en la
ventana principal. Si Chrome bloquea la ventana emergente, permití las
ventanas emergentes para `127.0.0.1` y volvé a pulsar `J`.

Los botones de escanear, calibrar y lanzar se habilitan recién con la cámara
y la ventana de proyección listas. Debajo de "Preparar la pared", el panel
muestra siempre el siguiente paso y resalta el botón que lo destraba.

Con la cámara y la proyección listas:

1. Pulsá `S` para escanear los post-its. La ventana del proyector se pone
   negra antes de tomar la imagen.
2. Pulsá `C` para calibrar. En la ventana del proyector, llevá la cruz al
   centro de cada post-it de esquina y confirmá las cuatro esquinas en el
   orden indicado. Revisá que los contornos proyectados coincidan con los
   papeles.
3. Ajustá la dirección desde el control principal y lanzá con `Espacio`.
   Tras ganar o perder, `R` inicia otro intento con el mismo tablero. Si
   moviste post-its, pulsá `S` para releerlos antes del siguiente intento.

Si se cierra la ventana del proyector durante un intento, la pelota queda en
pausa. Reabrila con `J` y pulsá **Continuar intento** (o `Espacio`). Con el
intento en pausa también podés releer los post-its, recalibrar o empezar otro
intento con `R`.

La vista de cámara sigue en directo durante la partida. El tablero del juego
se lee solo al escanear; las luces de detección en vivo son una guía previa.
Si movés la cámara, la pared o el proyector, repetí la calibración.

## Colores de los post-its

Debajo del video hay una fila por tipo de post-it: inicio, llegada, rebotes y
esquinas. Cada una tiene:

- **Luz**: se enciende con el color elegido cuando la cámara ve lo que el
  escaneo necesita (1 inicio, 1 llegada, 4 esquinas; cualquier cantidad de
  rebotes). Queda en ámbar si ve una cantidad incorrecta y apagada si no ve
  ninguno. Al lado figura la cuenta, por ejemplo `4/4`.
- **Cuadrado de color**: abre el selector de color de Chrome. En macOS, ese
  selector trae su propia lupa para tomar un color de la pantalla.
- **Gotero**: pulsalo y tocá el post-it en la imagen de la cámara. Toma el
  color promedio de un cuadrito de 7×7 píxeles, igual que lo ve el detector.
  `Esc` cancela.

Sobre la imagen, cada post-it detectado se ilumina con un halo de su color.
Así se ve qué papel exacto cuenta como cada tipo. **Colores originales**
vuelve a los colores por defecto. Los colores quedan guardados en
`runs/palette.json` y se mantienen al reiniciar el servidor.

Límites:

- Dos tipos no pueden tener tonos casi iguales: tienen que estar a 16° o más
  en el círculo cromático.
- Se rechaza un color casi gris o muy oscuro, porque la pared o las sombras
  se confundirían con él.
- Un tono vecino a dos tipos se reparte por cercanía, así un papel nunca
  cuenta para dos.
- Con la proyección del juego encendida, los contornos proyectados pueden
  iluminar papeles de más. El escaneo apaga la proyección antes de leer.
- Después de cambiar un color, escaneá otra vez para aplicarlo al tablero.

## Montaje

1. Usá una pared plana. Colocá los cuatro post-its de esquina cerca de las
   cuatro esquinas del área proyectada. La webcam debe verlos a todos y el
   proyector debe iluminar todo el campo.
2. Poné el inicio, la llegada y los rebotes dentro de ese rectángulo. Dejá
   espacio entre papeles y una separación de al menos el radio de la pelota
   respecto de los bordes. Revisá que las cuatro luces debajo del video estén
   encendidas antes de escanear.
3. Fijá webcam y proyector. Usá iluminación ambiente estable y configurá el
   proyector como pantalla extendida de macOS. Si se mueve cualquiera de los
   dos, pulsá `C` para recalibrar.

## Modo clásico de pantalla completa

La aplicación original de Pygame sigue disponible. Desde esta carpeta:

```sh
./.venv/bin/python main.py
```

Por defecto se abre en la segunda pantalla (índice `1`) si existe y usa
la webcam `0`. En este Mac, AVFoundation enumera la cámara FaceTime como
`0` y la Logitech HD Pro Webcam C920 como `1`. Para usar la C920, abrí
**Terminal** y ejecutá:

```sh
cd /Users/tobiasarraiza/Documents/Codex/2026-10-02/bien/outputs/postit-wall-game
./.venv/bin/python main.py --camera 1
```

Cuando macOS solicite acceso a la cámara para Terminal, elegí **Permitir**.
Si no aparece el aviso, revisá Ajustes del Sistema → Privacidad y seguridad
→ Cámara y activá Terminal si figura allí. El permiso corresponde a la
aplicación que inicia Python.

## Calibración y partida en el modo clásico

El modo clásico usa siempre los colores por defecto: inicio amarillo, llegada
naranja, rebotes verdes y esquinas fucsia. Los colores elegidos en la
interfaz web no se aplican a este modo.

- `A` calibra automáticamente: el proyector muestra puntos blancos de
  coordenadas conocidas, la webcam los localiza y se calcula una homografía.
  Otros cuatro puntos verifican el error. Si la webcam no ve bastantes puntos,
  el programa ofrece calibración manual.
- `M` calibra manualmente: usá las flechas para mover una cruz hasta el centro
  de cada post-it fucsia, en el orden indicado. `Shift` reduce la velocidad y
  `Enter` confirma cada cruz.
- Antes de jugar, revisá que los contornos proyectados coincidan con los
  post-its y que la cuadrícula esté alineada. `Enter` acepta; `A` o `M` repite
  la calibración.
- `←` y `→` cambian la dirección de lanzamiento. `Espacio` lanza la pelota.
  La pelota rebota en los verdes, gana al tocar el naranja y pierde al salir
  del campo. `R` reinicia con el mismo tablero.
- Para **mover post-its entre intentos**, detené el intento y pulsá `S`. La
  proyección queda negra brevemente, se vuelve a leer el tablero y aparece una
  pelota nueva en el amarillo. `C` recalibra si se movió el equipo. `Esc`
  cierra el juego.

La calibración automática requiere que los puntos blancos proyectados sean
visibles para la webcam. Con luz muy intensa, sombras o exposición automática
variable, usá `M`. La calibración supone pared plana; una lente con mucha
distorsión puede necesitar corrección óptica antes de mapear puntos.

## Pruebas y evidencia

Las pruebas son sintéticas y se ejecutan sin hardware. Requieren Node.js
además de Python para validar la interfaz del navegador:

```sh
./scripts/verify.sh
```

El script ejecuta estas pruebas y evaluaciones:

```sh
./.venv/bin/python -m unittest discover -s . -p 'test_*.py' -q
./.venv/bin/python -m services.calibration.evals.synthetic
./.venv/bin/python -m services.vision.eval
./.venv/bin/python -m services.physics.evals.scenarios
./.venv/bin/python -m services.runtime.eval
./.venv/bin/python -m services.web.backend.eval
./.venv/bin/python -m tests.eval_web_flow
node --test services/web/tests/*.test.mjs
node services/web/evals/flows.mjs
```

`services/web/evals/flows.mjs` comprueba que cada botón del panel coincida
con lo que el backend acepta en cada fase del juego. Si cambiás reglas del
backend o la forma de `/api/state`, regenerá la matriz que usa:
`./.venv/bin/python -m services.web.contract.matrix`. Una prueba falla
mientras la matriz guardada no coincida con el backend.

La prueba física que falta hacer en la pared es revisar la cuadrícula sobre
papeles reales y confirmar que el error máximo mostrado en la calibración
  automática sea menor que la mitad del radio de la pelota. Cada calibración, lectura de
tablero e intento registra valores en `runs/events.jsonl` para poder revisar
errores de alineación, cantidad de rebotes y tiempo de llegada.

## Estructura

- `contracts/`: datos y coordenadas compartidos.
- `services/vision/`: segmentación de los post-its con el proyector en negro.
- `services/calibration/`: puntos proyectados y homografía cámara → proyector.
- `services/physics/`: pelota y colisiones en píxeles del proyector.
- `services/runtime/`: captura de cámara, pantalla, controles y registro.
- `services/web/backend/`: servidor local, estado del juego y rutas HTTP.
- `services/web/ui/`: panel de control y ventana de proyección del navegador.
- `services/web/contract/`: matriz de estados del backend que leen las
  pruebas y la evaluación del navegador.
- `main.py`: entrada del programa.
- `web.py`: entrada de la interfaz web.

Cada servicio tiene sus propias pruebas, evaluación y explicación. El hook de
Git ejecuta las pruebas locales antes de cada commit; si clonás el proyecto,
activá el hook con `git config core.hooksPath .githooks`.
