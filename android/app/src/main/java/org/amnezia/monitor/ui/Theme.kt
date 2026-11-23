package org.amnezia.monitor.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

// Neon Latex: тёмный неоново-фиолетовый фон, стеклянные (полупрозрачные) карточки
val Bg = Color(0xFF07051A)
val Card = Color(0xB8160E34)
val Card2 = Color(0xFF2A2150)
val Line = Color(0x33BEA0FF)
val TextMain = Color(0xFFECE8FF)
val Muted = Color(0xFFA59DCC)
val Green = Color(0xFF34D399)
val Red = Color(0xFFF87171)
val Amber = Color(0xFFFBBF24)
val Indigo = Color(0xFFA855F7)
val Cyan = Color(0xFF22D3EE)
val TrafficDown = Color(0xFF60A5FA)
val TrafficUp = Color(0xFFC084FC)
val Petal = Color(0xFFFF9CCF)

val GlassBorder = BorderStroke(1.dp, Line)

private val Colors = darkColorScheme(
    primary = Indigo,
    onPrimary = Color(0xFF0B0726),
    secondary = Cyan,
    onSecondary = Color(0xFF0B0726),
    tertiary = Green,
    background = Bg,
    onBackground = TextMain,
    surface = Color(0xFF140C33),
    onSurface = TextMain,
    surfaceVariant = Card2,
    onSurfaceVariant = Muted,
    surfaceContainerHigh = Color(0xFF1C1240),
    error = Red,
    onError = Color(0xFF1A0A0A),
    outline = Line,
)

@Composable
fun AmneziaTheme(content: @Composable () -> Unit) {
    // Экраны и карточки полупрозрачные (фон страны просвечивает), а contentColorFor() для
    // нестандартных цветов берёт LocalContentColor — по умолчанию он чёрный. Задаём светлый текст.
    MaterialTheme(colorScheme = Colors, typography = Typography()) {
        CompositionLocalProvider(LocalContentColor provides TextMain, content = content)
    }
}
