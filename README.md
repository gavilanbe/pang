# 🎯 Pang Deluxe
Un tributo a Pang / Buster Bros para la terminal: revienta bolas con tu arpón antes de que te aplasten.

## ✨ Características
- Arte de bloques, partículas de chispas, combos y un motor chiptune hecho a mano
- Muchos powerups y armas: arpón, gancho pegajoso, pistola de pulsos, rayo ancho, doble, perforante, triple, espejo, escudo, imán, fantasma, auto-apuntado, bomba…
- Variantes de bola: pincho, hielo, explosiva, magnética, oro y jefes con HP
- 5 modos de juego: Clásico, Contrarreloj, Supervivencia, Boss Rush y Diario (semilla determinista)
- 3 dificultades y 4 personajes con estadísticas distintas
- Mundos temáticos (Espacio, Mar, Volcán, Neón) con gravedad y peligros propios (pinchos, plataformas)
- Enemigos (cangrejo, murciélago), logros y persistencia top-10 por modo
- Opciones de accesibilidad: modo daltónico, mute, ocultar puntuación
- Solo librería estándar de Python (audio vía ctypes si hay backend disponible)

## 🚀 Cómo jugar / ejecutar
```bash
python3 pang.py
```
Requiere Python 3.8+ y una terminal de al menos 64x22 con soporte Unicode. Sin dependencias externas; el guardado vive en `~/.pang_save.json`.

## 🎮 Controles
| Tecla | Acción |
| --- | --- |
| `←` `→` / `A` `D` | Mover |
| `Espacio` | Disparar |
| `P` / `Esc` | Pausa |
| `M` | Silenciar audio |
| `C` | Overlays daltónicos |
| `H` | Ocultar puntuación |
| `1` / `2` | (Título) dificultad / personaje |
| `Q` | Salir / volver |

## 🛠️ Tecnología
- Python 3.8+
- Librería `curses` (terminal)
- Motor de audio chiptune propio vía `ctypes` (libsoundio/portaudio si están presentes)

## 📦 Parte de mi colección de juegos
Uno más de mis juegos de terminal hechos por hobby. 🎮
