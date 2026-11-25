package org.amnezia.monitor.ui

import android.provider.Settings
import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipPath
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.amnezia.monitor.data.ThemeInfo
import kotlin.math.PI
import kotlin.math.sin

/** Анимации включены: настройка «Анимации» и системное «Убрать анимацию» (масштаб аниматора 0). */
val LocalAnimations = compositionLocalOf { true }
val LocalAccent = compositionLocalOf { Indigo }
val LocalAccent2 = compositionLocalOf { Cyan }

@Composable
fun systemAnimationsOn(): Boolean {
    val ctx = LocalContext.current
    return remember {
        runCatching { Settings.Global.getFloat(ctx.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) }
            .getOrDefault(1f) > 0f
    }
}

private val still = mutableFloatStateOf(0f)

/** 0..1..0 — плавное «туда-обратно»; без анимаций — константа (ничего не перерисовывается). */
@Composable
private fun pingPong(periodMs: Int, enabled: Boolean, label: String): State<Float> {
    if (!enabled) return still
    val tr = rememberInfiniteTransition(label = label)
    return tr.animateFloat(0f, 1f, infiniteRepeatable(tween(periodMs, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = label)
}

@Composable
private fun loop(periodMs: Int, enabled: Boolean, label: String): State<Float> {
    if (!enabled) return still
    val tr = rememberInfiniteTransition(label = label)
    return tr.animateFloat(0f, 1f, infiniteRepeatable(tween(periodMs, easing = LinearEasing), RepeatMode.Restart), label = label)
}

fun heroineRes(key: String): Int = ThemeRes.HEROINES[key] ?: ThemeRes.HEROINES.getValue("default")
fun backgroundRes(key: String): Int = ThemeRes.BACKGROUNDS[key] ?: ThemeRes.BACKGROUNDS.getValue("default")

/**
 * Фон всего приложения: город страны в цветах флага (crossfade при смене сервера, медленный «Ken Burns»),
 * затемнение, пульсирующее свечение акцента и лепестки сакуры.
 */
@Composable
fun NeonBackground(theme: ThemeInfo, accent: Color, content: @Composable BoxScope.() -> Unit) {
    val anim = LocalAnimations.current
    Box(Modifier.fillMaxSize().background(Bg)) {
        val burns = pingPong(48_000, anim, "kenburns")
        Crossfade(targetState = theme.key, animationSpec = tween(if (anim) 1200 else 0), label = "bg") { key ->
            Image(
                painter = painterResource(backgroundRes(key)),
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize().graphicsLayer {
                    val t = burns.value
                    scaleX = 1.05f + 0.10f * t
                    scaleY = scaleX
                    translationX = -0.025f * size.width * t
                    translationY = -0.015f * size.height * t
                },
            )
        }
        Box(
            Modifier.fillMaxSize().background(
                Brush.verticalGradient(
                    0f to Color(0x9907051A), 0.3f to Color(0x6607051A), 0.65f to Color(0xB307051A), 1f to Color(0xF207051A),
                ),
            ),
        )
        val pulse = pingPong(4_500, anim, "glow")
        Canvas(Modifier.fillMaxSize()) {
            val a = 0.16f + 0.12f * pulse.value
            drawCircle(
                Brush.radialGradient(listOf(accent.copy(alpha = a), Color.Transparent), center = Offset(size.width * 0.8f, size.height * 0.18f), radius = size.width * 0.9f),
                radius = size.width * 0.9f, center = Offset(size.width * 0.8f, size.height * 0.18f),
            )
        }
        if (anim) Petals()
        content()
    }
}

private class PetalSeed(val x: Float, val speed: Float, val offset: Float, val size: Float, val drift: Float, val spin: Float)

@Composable
fun Petals(count: Int = 12) {
    val seeds = remember {
        val r = java.util.Random(1989)
        List(count) { PetalSeed(r.nextFloat(), 0.6f + r.nextFloat() * 0.7f, r.nextFloat(), 0.6f + r.nextFloat() * 0.6f, 20f + r.nextFloat() * 50f, 200f + r.nextFloat() * 300f) }
    }
    val t = loop(22_000, true, "petals")
    val density = LocalDensity.current
    val pw = with(density) { 12.dp.toPx() }
    Canvas(Modifier.fillMaxSize()) {
        val time = t.value
        for (s in seeds) {
            val p = (time * s.speed + s.offset) % 1f
            val y = p * (size.height + 80f) - 40f
            val x = s.x * size.width + sin(p * 2f * PI.toFloat() * 1.5f) * s.drift * density.density
            val w = pw * s.size
            rotate(p * s.spin, Offset(x, y)) {
                drawOval(
                    Brush.linearGradient(listOf(Color(0xFFFFD6EA), Petal), Offset(x - w / 2, y), Offset(x + w / 2, y + w * 0.8f)),
                    topLeft = Offset(x - w / 2, y - w * 0.4f), size = Size(w, w * 0.8f), alpha = 0.75f,
                )
            }
        }
    }
}

@Composable
fun FlagImage(country: String?, width: Dp, modifier: Modifier = Modifier) {
    val res = country?.let { ThemeRes.FLAGS[it] }
    val h = width * 0.75f
    val shape = RoundedCornerShape(width * 0.14f)
    if (res != null) {
        Image(
            painterResource(res), contentDescription = null, contentScale = ContentScale.Crop,
            modifier = modifier.size(width, h).clip(shape).border(1.dp, Color(0x38FFFFFF), shape),
        )
    } else {
        Box(modifier.size(width, h).clip(shape).background(Card2), contentAlignment = Alignment.Center) {
            Text("🌐", fontSize = (width.value * 0.42f).sp)
        }
    }
}

/** Флаг с точкой статуса в углу — как на вкладках панели ПК. */
@Composable
fun FlagWithDot(country: String?, status: String, width: Dp = 34.dp) {
    Box {
        FlagImage(country, width, Modifier.padding(end = 3.dp, bottom = 3.dp))
        Box(
            Modifier.align(Alignment.BottomEnd).size(11.dp).clip(CircleShape).background(Color(0xFF120A33)).padding(2.dp)
                .clip(CircleShape).background(statusColor(status)),
        )
    }
}

/** Цвет дуги нагрузки: акцент флага → янтарный (≥70%) → красный (≥90%); офлайн — серый. */
fun loadTint(pct: Double?, up: Boolean, accent: Color): Color {
    if (!up) return Color(0xFF6F6898)
    val p = pct ?: return accent
    return when {
        p >= 90.0 -> Color(0xFFF87171)
        p >= 70.0 -> Color(0xFFFBBF24)
        else -> accent
    }
}

/**
 * Сцена экрана сервера: героиня на всю высоту карточки (растёт вместе с экраном),
 * в круге за ней — мягкий флаг страны и кольца нагрузки (снаружи CPU, внутри память).
 */
@Composable
fun HeroStage(
    theme: ThemeInfo,
    accent: Color,
    accent2: Color,
    modifier: Modifier = Modifier,
    cpu: Double? = null,
    mem: Double? = null,
    status: String? = null,
) {
    val anim = LocalAnimations.current
    val breathe = pingPong(2_750, anim, "breathe")
    val density = LocalDensity.current
    val screenH = LocalConfiguration.current.screenHeightDp.dp
    val heroH = (screenH * 0.60f).coerceIn(320.dp, 780.dp)
    val up = status == "up"
    val cpuA by animateFloatAsState((if (up) cpu?.toFloat() else null)?.coerceIn(0f, 100f) ?: 0f, tween(if (anim) 800 else 0), label = "cpuRing")
    val memA by animateFloatAsState((if (up) mem?.toFloat() else null)?.coerceIn(0f, 100f) ?: 0f, tween(if (anim) 800 else 0), label = "memRing")
    val shape = RoundedCornerShape(24.dp)
    val flagRes = theme.country.takeIf { it.isNotEmpty() }?.let { ThemeRes.FLAGS[it] }
    Box(
        modifier.fillMaxWidth().height(heroH).clip(shape).border(1.dp, accent.copy(alpha = 0.35f), shape)
            .background(Brush.verticalGradient(listOf(Color(0x0DFFFFFF), Color(0x5907051A)))),
    ) {
        BoxWithConstraints(Modifier.fillMaxSize()) {
            val side = minOf(maxWidth * 0.92f, maxHeight * 0.78f)
            Box(Modifier.align(Alignment.Center).size(side).offset(y = -maxHeight * 0.04f)) {
                Crossfade(targetState = flagRes, animationSpec = tween(if (anim) 700 else 0), label = "gflag") { res ->
                    if (res != null) {
                        Image(
                            painterResource(res), contentDescription = null, contentScale = ContentScale.Crop,
                            modifier = Modifier.fillMaxSize().clip(CircleShape).alpha(0.40f),
                        )
                    } else {
                        Box(
                            Modifier.fillMaxSize().clip(CircleShape).background(
                                Brush.radialGradient(listOf(accent.copy(alpha = 0.50f), accent.copy(alpha = 0.16f), Color(0xCC07051A))),
                            ),
                        )
                    }
                }
                Canvas(Modifier.fillMaxSize()) {
                    val c = Offset(size.width / 2f, size.height / 2f)
                    val r = size.minDimension / 2f
                    drawCircle(Brush.radialGradient(listOf(Color(0x14000000), Color(0x8A07051A), Color(0xF207051A)), c, r), r, c)
                    val clip = Path().apply { addOval(androidx.compose.ui.geometry.Rect(c, r * 0.98f)) }
                    clipPath(clip) {
                        val step = 13.dp.toPx()
                        var x = c.x - r
                        while (x < c.x + r) { drawLine(Color(0x24EBE1FF), Offset(x, c.y - r), Offset(x, c.y + r), 1f); x += step }
                        var y = c.y - r
                        while (y < c.y + r) { drawLine(Color(0x1AEBE1FF), Offset(c.x - r, y), Offset(c.x + r, y), 1f); y += step }
                    }
                    val track = if (up) Color(0x33EBE1FF) else Color(0x336F6898)
                    fun ring(radius: Float, width: Float, pct: Float, color: Color) {
                        drawCircle(track, radius, c, style = Stroke(width))
                        if (pct > 0.4f) {
                            drawArc(
                                color = color,
                                startAngle = -90f,
                                sweepAngle = 360f * (pct / 100f),
                                useCenter = false,
                                topLeft = Offset(c.x - radius, c.y - radius),
                                size = Size(radius * 2, radius * 2),
                                style = Stroke(width, cap = StrokeCap.Round),
                            )
                        }
                    }
                    ring(r - 5.dp.toPx(), 5.dp.toPx(), cpuA, loadTint(cpu, up, accent))
                    ring(r - 13.dp.toPx(), 3.dp.toPx(), memA, loadTint(mem, up, accent))
                }
            }
        }
        Crossfade(targetState = theme.key, animationSpec = tween(if (anim) 800 else 0), label = "hero", modifier = Modifier.fillMaxSize()) { key ->
            Image(
                painterResource(heroineRes(key)), contentDescription = null,
                contentScale = ContentScale.Fit, alignment = Alignment.BottomCenter,
                modifier = Modifier.fillMaxSize().graphicsLayer {
                    val t = breathe.value
                    translationY = -t * with(density) { 6.dp.toPx() }
                    scaleX = 1f + 0.01f * t
                    scaleY = scaleX
                    transformOrigin = androidx.compose.ui.graphics.TransformOrigin(0.5f, 1f)
                },
            )
        }
        Crossfade(targetState = theme.country, animationSpec = tween(if (anim) 600 else 0), label = "flag", modifier = Modifier.align(Alignment.TopEnd).padding(14.dp)) { cc ->
            if (cc.isNotEmpty()) FlagImage(cc, 64.dp)
        }
        Column(
            Modifier.align(Alignment.BottomCenter).fillMaxWidth()
                .background(Brush.verticalGradient(listOf(Color.Transparent, Color(0xEB05040F))))
                .padding(top = 28.dp, bottom = 12.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            if (!up) {
                Text("нет данных", color = Color(0xFF6F6898), letterSpacing = 1.2.sp, fontSize = 11.sp)
            } else {
                androidx.compose.foundation.layout.Row {
                    Text("CPU ${if (cpu == null) "—" else "${cpu.toInt()}%"}", color = loadTint(cpu, true, accent2), letterSpacing = 1.2.sp, fontSize = 11.sp)
                    Text("  ·  пам. ${if (mem == null) "—" else "${mem.toInt()}%"}", color = loadTint(mem, true, accent2), letterSpacing = 1.2.sp, fontSize = 11.sp)
                }
            }
            Text(
                (theme.countryName.ifBlank { "Amnezia" }).uppercase(), color = Color.White, fontWeight = FontWeight.Bold,
                letterSpacing = 3.sp, fontSize = 15.sp, textAlign = TextAlign.Center,
            )
            Text(
                if (theme.country.isNotEmpty()) "сервер · ${theme.country.uppercase()}" else "neon latex",
                color = accent2, letterSpacing = 2.sp, fontSize = 11.sp,
            )
        }
    }
}

