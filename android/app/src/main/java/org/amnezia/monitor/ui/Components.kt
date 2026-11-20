package org.amnezia.monitor.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import org.amnezia.monitor.data.fmtPct
import org.amnezia.monitor.data.statusLabel

fun statusColor(status: String): Color = when (status) {
    "up" -> Green
    "down" -> Red
    else -> Muted
}

@Composable
fun StatusChip(status: String, modifier: Modifier = Modifier, label: String? = null) {
    val color = statusColor(status)
    Row(
        modifier = modifier
            .clip(RoundedCornerShape(999.dp))
            .background(color.copy(alpha = 0.14f))
            .padding(horizontal = 10.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Box(Modifier.size(8.dp).clip(CircleShape).background(color))
        Text(label ?: statusLabel(status), color = color, style = MaterialTheme.typography.labelMedium)
    }
}

@Composable
fun Meter(label: String, value: Double?, modifier: Modifier = Modifier) {
    val frac = ((value ?: 0.0) / 100.0).toFloat().coerceIn(0f, 1f)
    val color = when {
        value == null -> Muted
        value >= 90 -> Red
        value >= 75 -> Amber
        else -> Indigo
    }
    Column(modifier, verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(label, color = Muted, style = MaterialTheme.typography.labelMedium)
            Text(fmtPct(value), color = TextMain, style = MaterialTheme.typography.labelMedium)
        }
        Box(
            Modifier
                .fillMaxWidth()
                .height(6.dp)
                .clip(RoundedCornerShape(4.dp))
                .background(Card2),
        ) {
            Box(
                Modifier
                    .fillMaxHeight()
                    .fillMaxWidth(if (value == null) 0f else frac)
                    .background(color),
            )
        }
    }
}

@Composable
fun LineChart(
    values: List<Double?>,
    color: Color,
    modifier: Modifier = Modifier,
    yMax: Float? = null,
) {
    Canvas(modifier.height(112.dp).fillMaxWidth()) {
        if (values.isEmpty()) return@Canvas
        val nums = values.map { it?.toFloat() }
        val peak = (yMax ?: nums.filterNotNull().maxOrNull() ?: 1f).coerceAtLeast(0.001f)
        val n = nums.size
        val step = if (n <= 1) 0f else size.width / (n - 1)
        val path = Path()
        var pen = false
        nums.forEachIndexed { i, v ->
            if (v == null) {
                pen = false
                return@forEachIndexed
            }
            val x = i * step
            val y = size.height - (v / peak).coerceIn(0f, 1f) * (size.height - 4f) - 2f
            if (!pen) {
                path.moveTo(x, y)
                pen = true
            } else {
                path.lineTo(x, y)
            }
        }
        drawPath(path, color, style = Stroke(width = 2.5.dp.toPx(), cap = StrokeCap.Round))
    }
}

@Composable
fun DualBarChart(
    down: List<Double?>,
    up: List<Double?>,
    modifier: Modifier = Modifier,
) {
    Canvas(modifier.height(112.dp).fillMaxWidth()) {
        val n = maxOf(down.size, up.size)
        if (n == 0) return@Canvas
        val peak = (0 until n).maxOf { i ->
            maxOf(down.getOrNull(i) ?: 0.0, up.getOrNull(i) ?: 0.0)
        }.toFloat().coerceAtLeast(1f)
        val group = size.width / n
        val gap = (group * 0.2f).coerceAtLeast(1f)
        val barW = ((group - gap) / 2f).coerceAtLeast(1f)
        for (i in 0 until n) {
            val d = (down.getOrNull(i) ?: 0.0).toFloat().coerceAtLeast(0f)
            val u = (up.getOrNull(i) ?: 0.0).toFloat().coerceAtLeast(0f)
            val x0 = i * group
            val hd = (d / peak).coerceIn(0f, 1f) * size.height
            val hu = (u / peak).coerceIn(0f, 1f) * size.height
            drawRect(TrafficDown, topLeft = Offset(x0, size.height - hd), size = androidx.compose.ui.geometry.Size(barW, hd))
            drawRect(TrafficUp, topLeft = Offset(x0 + barW, size.height - hu), size = androidx.compose.ui.geometry.Size(barW, hu))
        }
    }
}

@Composable
fun Sparkline(values: List<Double>, color: Color, modifier: Modifier = Modifier) {
    Canvas(modifier.height(28.dp).width(88.dp)) {
        if (values.isEmpty()) return@Canvas
        val peak = values.maxOrNull()?.toFloat()?.coerceAtLeast(1f) ?: 1f
        val step = if (values.size <= 1) 0f else size.width / (values.size - 1)
        val path = Path()
        values.forEachIndexed { i, v ->
            val x = i * step
            val y = size.height - (v.toFloat() / peak).coerceIn(0f, 1f) * size.height
            if (i == 0) path.moveTo(x, y) else path.lineTo(x, y)
        }
        drawPath(path, color, style = Stroke(width = 1.6.dp.toPx(), cap = StrokeCap.Round))
    }
}

@Composable
fun SectionLabel(text: String, trailing: String? = null) {
    Row(Modifier.fillMaxWidth().padding(top = 8.dp, bottom = 6.dp), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(text, color = Muted, style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
        if (trailing != null) Text(trailing, color = Muted, style = MaterialTheme.typography.labelMedium)
    }
}
