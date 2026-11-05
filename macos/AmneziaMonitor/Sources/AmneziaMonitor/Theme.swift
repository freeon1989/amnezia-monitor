import SwiftUI

enum Theme {
    static let bg = Color(red: 11 / 255, green: 16 / 255, blue: 32 / 255)
    static let card = Color(red: 20 / 255, green: 28 / 255, blue: 56 / 255)
    static let card2 = Color(red: 24 / 255, green: 34 / 255, blue: 74 / 255)
    static let line = Color(red: 36 / 255, green: 48 / 255, blue: 99 / 255)
    static let text = Color(red: 231 / 255, green: 236 / 255, blue: 255 / 255)
    static let muted = Color(red: 141 / 255, green: 151 / 255, blue: 194 / 255)
    static let green = Color(red: 52 / 255, green: 211 / 255, blue: 153 / 255)
    static let red = Color(red: 248 / 255, green: 113 / 255, blue: 113 / 255)
    static let amber = Color(red: 251 / 255, green: 191 / 255, blue: 36 / 255)
    static let indigo = Color(red: 129 / 255, green: 140 / 255, blue: 248 / 255)
    static let cyan = Color(red: 34 / 255, green: 211 / 255, blue: 238 / 255)
    static let down = Color(red: 96 / 255, green: 165 / 255, blue: 250 / 255)
    static let up = Color(red: 167 / 255, green: 139 / 255, blue: 250 / 255)
}

struct StatusChip: View {
    var status: String
    var label: String?
    var body: some View {
        let text = label ?? statusLabel(status)
        let color: Color = status == "up" ? Theme.green : (status == "down" ? Theme.red : Theme.amber)
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(0.15), in: Capsule())
    }
}

struct Meter: View {
    var title: String
    var value: Double?
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title).foregroundStyle(Theme.muted).font(.caption)
                Spacer()
                Text(fmtPct(value)).foregroundStyle(Theme.text).font(.caption.monospacedDigit())
            }
            GeometryReader { geo in
                let pct = CGFloat(min(max(value ?? 0, 0), 100) / 100)
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.line)
                    Capsule().fill(value == nil ? Theme.muted : (pct > 0.85 ? Theme.red : Theme.indigo))
                        .frame(width: max(4, geo.size.width * pct))
                }
            }
            .frame(height: 6)
        }
    }
}

struct ErrorBanner: View {
    var text: String
    var body: some View {
        Text(text)
            .foregroundStyle(Theme.red)
            .font(.callout)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(Theme.red.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct SectionTitle: View {
    var title: String
    var trailing: String?
    var body: some View {
        HStack {
            Text(title).font(.headline).foregroundStyle(Theme.text)
            Spacer()
            if let trailing = trailing {
                Text(trailing).font(.caption).foregroundStyle(Theme.muted)
            }
        }
    }
}

struct LineChart: View {
    var values: [Double?]
    var color: Color
    var yMax: Double
    var body: some View {
        GeometryReader { geo in
            Path { path in
                var drawing = false
                let n = max(values.count - 1, 1)
                for (i, v) in values.enumerated() {
                    guard let v = v else {
                        drawing = false
                        continue
                    }
                    let x = geo.size.width * CGFloat(i) / CGFloat(n)
                    let y = geo.size.height * (1 - CGFloat(min(max(v, 0), yMax) / yMax))
                    if drawing {
                        path.addLine(to: CGPoint(x: x, y: y))
                    } else {
                        path.move(to: CGPoint(x: x, y: y))
                        drawing = true
                    }
                }
            }
            .stroke(color, style: StrokeStyle(lineWidth: 1.6, lineJoin: .round))
        }
        .frame(height: 64)
    }
}

struct Sparkline: View {
    var values: [Double]
    var color: Color
    var body: some View {
        GeometryReader { geo in
            let maxV = max(values.max() ?? 1, 1)
            Path { path in
                for (i, v) in values.enumerated() {
                    let x = geo.size.width * CGFloat(i) / CGFloat(max(values.count - 1, 1))
                    let y = geo.size.height * (1 - CGFloat(v / maxV))
                    if i == 0 { path.move(to: CGPoint(x: x, y: y)) }
                    else { path.addLine(to: CGPoint(x: x, y: y)) }
                }
            }
            .stroke(color, lineWidth: 1.4)
        }
        .frame(width: 72, height: 28)
    }
}

struct TrafficBars: View {
    var down: [Double?]
    var up: [Double?]
    var body: some View {
        GeometryReader { geo in
            let n = max(down.count, up.count, 1)
            let peak = max(down.compactMap { $0 }.max() ?? 1, up.compactMap { $0 }.max() ?? 1, 1)
            HStack(alignment: .bottom, spacing: 2) {
                ForEach(0..<n, id: \.self) { i in
                    let d = i < down.count ? (down[i] ?? 0) : 0
                    let u = i < up.count ? (up[i] ?? 0) : 0
                    VStack(spacing: 1) {
                        Spacer(minLength: 0)
                        RoundedRectangle(cornerRadius: 1)
                            .fill(Theme.down)
                            .frame(height: geo.size.height * 0.48 * CGFloat(d / peak))
                        RoundedRectangle(cornerRadius: 1)
                            .fill(Theme.up)
                            .frame(height: geo.size.height * 0.48 * CGFloat(u / peak))
                    }
                }
            }
        }
        .frame(height: 72)
    }
}
