package org.amnezia.monitor.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

val Bg = Color(0xFF0B1020)
val Card = Color(0xFF141C38)
val Card2 = Color(0xFF18224A)
val Line = Color(0xFF243063)
val TextMain = Color(0xFFE7ECFF)
val Muted = Color(0xFF8D97C2)
val Green = Color(0xFF34D399)
val Red = Color(0xFFF87171)
val Amber = Color(0xFFFBBF24)
val Indigo = Color(0xFF818CF8)
val Cyan = Color(0xFF22D3EE)
val TrafficDown = Color(0xFF60A5FA)
val TrafficUp = Color(0xFFA78BFA)

private val Colors = darkColorScheme(
    primary = Indigo,
    onPrimary = Color(0xFF0B1020),
    secondary = Cyan,
    onSecondary = Color(0xFF0B1020),
    tertiary = Green,
    background = Bg,
    onBackground = TextMain,
    surface = Card,
    onSurface = TextMain,
    surfaceVariant = Card2,
    onSurfaceVariant = Muted,
    error = Red,
    onError = Color(0xFF1A0A0A),
    outline = Line,
)

@Composable
fun AmneziaTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = Colors, typography = Typography(), content = content)
}
