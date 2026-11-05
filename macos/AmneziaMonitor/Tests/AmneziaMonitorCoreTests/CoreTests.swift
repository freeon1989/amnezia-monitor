import CZlibUncompress
import Foundation
import XCTest
@testable import AmneziaMonitorCore

final class CoreTests: XCTestCase {
    func testVpnLinkKeepsSshSecretAndDropsVpnKeys() throws {
        let json = """
        {"hostName":"203.0.113.11","userName":"root","password":"s3cret-ssh","port":22,\
        "description":"Дом","containers":[{"container":"amnezia-awg",\
        "awg":{"last_config":"WIREGUARD-PRIVATE-KEY-DATA"}}]}
        """
        let link = "vpn://" + b64Url(qCompress(Data(json.utf8)))
        let servers = try AmneziaImport.parse("смотрите ссылку \(link) спасибо")
        XCTAssertEqual(servers.count, 1)
        XCTAssertEqual(servers[0].name, "Дом")
        XCTAssertEqual(servers[0].host, "203.0.113.11")
        XCTAssertEqual(servers[0].port, 22)
        XCTAssertEqual(servers[0].password, "s3cret-ssh")
        XCTAssertTrue(servers[0].privateKey.isEmpty)
        let dumped = String(describing: servers[0])
        XCTAssertFalse(dumped.contains("WIREGUARD"))
    }

    func testPythonZlibFixtureDecodes() throws {
        // Независимый zlib (уровень 8), не компрессор из этого репозитория.
        let link = "vpn://AAAAxHjaRYpNCsIwFITv8tZN6c8uu0CLFEGkVEVEJLTRBmxS8iIFS2_hccQz9Ei-guBiBuabb4TWot_ITgGHJErDKIzjlAIBPFC53-Os9UR6iThY1xDBtHbKM8R24dZ54EkSQKOwdrr32hqS5tf8md8k1NZ4qY1yCPw0_ic5sjPqqSWTw43EpfkId4n-QtZV04RDUearnSgzti2Lvahyts6PLBOVgGk6T1_AXUEm"
        let servers = try AmneziaImport.parse(link)
        XCTAssertEqual(servers[0].name, "Дом")
        XCTAssertEqual(servers[0].password, "s3cret-ssh")
        XCTAssertFalse(String(describing: servers[0]).contains("WIREGUARD"))
    }

    func testBackupJsonArrayAndString() throws {
        let array = #"{"Servers/serversList":[{"hostName":"vpn.example.com","description":"DE","userName":"root","password":"pw"}]}"#
        let a = try AmneziaImport.parse(array)
        XCTAssertEqual(a[0].name, "DE")
        XCTAssertEqual(a[0].host, "vpn.example.com")
        XCTAssertEqual(a[0].password, "pw")
        let asString = #"{"Servers/serversList":"[{\"hostName\":\"203.0.113.20\",\"userName\":\"monitor\",\"password\":\"\",\"port\":2222,\"description\":\"NL\"}]"}"#
        let b = try AmneziaImport.parse(asString)
        XCTAssertEqual(b[0].name, "NL")
        XCTAssertEqual(b[0].port, 2222)
        XCTAssertEqual(b[0].user, "monitor")
        XCTAssertFalse(b[0].hasSshAuth)
    }

    func testWireguardConfImportsHostOnly() throws {
        let conf = """
        # Берлин
        [Interface]
        PrivateKey = SHOULD-NOT-STORE
        [Peer]
        Endpoint = 198.51.100.8:51820
        """
        let s = try AmneziaImport.parse(conf)
        XCTAssertEqual(s.count, 1)
        XCTAssertEqual(s[0].name, "Берлин")
        XCTAssertEqual(s[0].host, "198.51.100.8")
        XCTAssertEqual(s[0].port, 22)
        XCTAssertFalse(s[0].hasSshAuth)
        XCTAssertFalse(String(describing: s[0]).contains("SHOULD-NOT-STORE"))
    }

    func testSshKeyGoesToPrivateKeyField() throws {
        let json = #"{"hostName":"203.0.113.5","userName":"root","password":"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n","description":"k"}"#
        let s = try XCTUnwrap(AmneziaImport.parse(json).first)
        XCTAssertTrue(s.password.isEmpty)
        XCTAssertTrue(s.privateKey.contains("OPENSSH"))
    }

    func testMergeUpdatesSameHost() {
        let old = [ImportedServer(id: "id1", name: "old", host: "203.0.113.11", port: 22, user: "root", password: "oldpw")]
        let incoming = [ImportedServer(id: "", name: "Дом", host: "203.0.113.11", port: 22, user: "root", password: "newpw")]
        let merged = AmneziaImport.merge(current: old, incoming: incoming) { "new" }
        XCTAssertEqual(merged.count, 1)
        XCTAssertEqual(merged[0].id, "id1")
        XCTAssertEqual(merged[0].name, "Дом")
        XCTAssertEqual(merged[0].password, "newpw")
    }

    func testGarbageRejected() {
        XCTAssertThrowsError(try AmneziaImport.parse("привет, это не конфиг")) { error in
            XCTAssertTrue(String(describing: error).contains("не"))
        }
    }

    func testParsersIgnorePrivateKeyLineAndWhitelistCommands() {
        let dump = "awg0\tPRIVATE-SERVER-KEY\tpub\t39743\toff\n" +
            "awg0\tPEERPUB\tpreshared\t198.51.100.9:1000\t10.8.1.2/32\t100\t10\t20\t25\n"
        let peers = ProcParse.wgPeers(dump, nowSec: 120)
        XCTAssertEqual(peers.count, 1)
        XCTAssertEqual(peers[0].pubkey, "PEERPUB")
        XCTAssertTrue(peers[0].online)
        XCTAssertFalse(peers.map(\.pubkey).joined().contains("PRIVATE"))
        let mem = ProcParse.mem("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
        XCTAssertEqual(mem?.0 ?? -1, 75, accuracy: 0.01)
        let ps = ProcParse.dockerPs("amnezia-awg|Up 3 days|amnezia-awg\nnginx|Up|nginx\n")
        XCTAssertEqual(ps.map(\.name), ["amnezia-awg"])
        XCTAssertTrue(ProcParse.allowed("cat /proc/meminfo"))
        XCTAssertTrue(ProcParse.allowed("docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'"))
        XCTAssertTrue(ProcParse.allowed("docker exec amnezia-awg sh -c 'awg show all dump 2>/dev/null || wg show all dump'"))
        XCTAssertTrue(ProcParse.allowed("docker exec amnezia-dns sh -c 'tail -n 400 /var/log/unbound.log'"))
        XCTAssertTrue(ProcParse.allowed("sudo -n docker logs --tail 400 amnezia-dns"))
        XCTAssertFalse(ProcParse.allowed("docker restart amnezia-awg"))
        XCTAssertFalse(ProcParse.allowed("docker exec amnezia-awg sh -c 'rm -rf /'"))
        XCTAssertNil(ProcParse.safeName("amnezia-awg;rm"))
        XCTAssertTrue(ProcParse.allowed(ProcParse.dockerPsCommand("docker")))
        XCTAssertTrue(ProcParse.allowed(ProcParse.wgCommand(docker: "docker", name: "amnezia-awg")))
        XCTAssertTrue(ProcParse.allowed(ProcParse.clientsCommand(docker: "sudo -n docker", name: "amnezia-awg", type: "awg")))
        XCTAssertTrue(ProcParse.allowed(ProcParse.dnsLogCommand(docker: "docker", name: "amnezia-dns")))
        XCTAssertTrue(ProcParse.allowed(ProcParse.dnsDockerLogsCommand(docker: "sudo -n docker", name: "amnezia-dns")))
        XCTAssertFalse(ProcParse.allowed(ProcParse.wgCommand(docker: "docker", name: "amnezia-awg;reboot")))
    }

    func testParsesServerListAndNodata() throws {
        let raw = """
        [
          {"name":"test1","host":"203.0.113.11","status":"up","age":12,"tcp_ms":20,
           "cpu":15.5,"mem":40,"peers_online":2,"peers_total":5,"rx_bps":1000,"tx_bps":2000,"error":null},
          {"name":"пусто","host":"203.0.113.14","status":"nodata"}
        ]
        """
        let list = try ApiJson.parseServers(raw)
        XCTAssertEqual(list.count, 2)
        XCTAssertEqual(list[0].status, "up")
        XCTAssertEqual(list[0].peersOnline, 2)
        XCTAssertEqual(list[0].txBps ?? -1, 2000, accuracy: 0.01)
        XCTAssertEqual(list[1].status, "nodata")
        XCTAssertNil(list[1].cpu)
        XCTAssertNil(list[1].error)
    }

    func testParsesDetailSeriesUsersAndDns() throws {
        let raw = """
        {
          "name":"test1","host":"203.0.113.11",
          "current":{"status":"down","error":"SSH timeout","age":9,"tcp_ms":4,"ping_ms":3.2,
            "availability":99.1,"latency_avg":8,"uptime_s":3600,"load1":0.4,"nproc":2,
            "cpu":10,"mem":40,"mem_used":100,"mem_total":200,"rx_bps":1,"tx_bps":2,"stale":true},
          "containers":[{"name":"amnezia-awg","type":"awg","up":true,"status":"Up"}],
          "series":{"start":1,"end":2,"buckets":2,"cpu":[1,null],"mem":[2,3],"online":[1,0],
            "down_bytes":[10,0],"up_bytes":[0,5],"avail":[1,null]},
          "users":[{"client":"Дмитрий — телефон","pubkey":"abc…","container":"amnezia-awg",
            "online":false,"handshake_age":500,"endpoint":"198.51.100.9","rate_down":null,"rate_up":null,
            "spark":[1,2],"down_24h":100,"up_24h":50,"down_total":1000,"up_total":400}],
          "totals":{"down_24h":100,"up_24h":50},
          "dns":{"status":"ok","note":null,"queries":3,"top":[{"site":"youtube.com","n":2}],
            "per_user":[{"client":"Дмитрий — телефон","total":2,"top":[{"site":"youtube.com","n":2}]}]},
          "events":[{"ts":1800000000,"kind":"peer","message":"подключился"}]
        }
        """
        let d = try ApiJson.parseDetail(raw)
        XCTAssertEqual(d.current.status, "down")
        XCTAssertTrue(d.current.stale)
        XCTAssertEqual(d.current.error, "SSH timeout")
        XCTAssertNil(d.series?.cpu[1])
        XCTAssertEqual(d.series?.downBytes[0] ?? -1, 10, accuracy: 0.01)
        XCTAssertEqual(d.users[0].client, "Дмитрий — телефон")
        XCTAssertFalse(d.users[0].online)
        XCTAssertNil(d.users[0].rateDown)
        XCTAssertEqual(d.dns.top[0].site, "youtube.com")
        XCTAssertEqual(d.dns.perUser[0].total, 2)
        XCTAssertEqual(d.events[0].message, "подключился")
        XCTAssertTrue(d.containers[0].up)
    }

    func testNodataDetailAndErrorObject() throws {
        let nodata = #"{"name":"x","status":"nodata","users":[],"containers":[],"events":[],"series":null,"dns":{"status":"nodata","note":"Данных пока нет"}}"#
        let d = try ApiJson.parseDetail(nodata)
        XCTAssertEqual(d.current.status, "nodata")
        XCTAssertNil(d.series)
        XCTAssertEqual(d.dns.note, "Данных пока нет")
        XCTAssertThrowsError(try ApiJson.parseServers(#"{"error":"bad token"}"#)) { error in
            XCTAssertEqual((error as? ApiException)?.message, "bad token")
        }
    }

    func testUrlAndTokenRules() throws {
        XCTAssertEqual(try normalizeBaseUrl("192.168.1.104:8765"), "http://192.168.1.104:8765")
        XCTAssertEqual(try normalizeBaseUrl("http://192.168.1.104:8765/"), "http://192.168.1.104:8765")
        XCTAssertEqual(try normalizeBaseUrl("https://mon.example"), "https://mon.example")
        XCTAssertThrowsError(try normalizeBaseUrl("http://user:secret@192.168.1.104:8765"))
        XCTAssertThrowsError(try normalizeBaseUrl("http://192.168.1.104:8765?token=abc"))
        XCTAssertThrowsError(try normalizeBaseUrl("ftp://192.168.1.104"))
        XCTAssertEqual(try cleanToken("  "), "")
        XCTAssertThrowsError(try cleanToken("a\nb"))
    }

    func testFormatsMatchDashboardStyle() {
        XCTAssertEqual(fmtBytes(1024), "1.0 КБ")
        XCTAssertEqual(fmtRate(2000), "16 Кбит/с")
        XCTAssertEqual(fmtRate(nil), "—")
        XCTAssertTrue(friendlyError(ApiException(code: 403, message: "bad token")).contains("токен"))
    }

    func testPublicSettingsDropSecrets() throws {
        let settings = AppSettings(
            endpoints: [Endpoint(id: "1", label: "Дом", baseUrl: "http://127.0.0.1:8765", token: "SECRET-TOKEN")],
            activeId: "1",
            intervalSec: 5,
            imported: [ImportedServer(
                id: "s", name: "n", host: "203.0.113.11", port: 22, user: "root",
                password: "SECRET-SSH", privateKey: "SECRET-KEY"
            )]
        )
        let data = try SettingsCodec.encodePublic(settings)
        let text = String(data: data, encoding: .utf8) ?? ""
        XCTAssertFalse(text.contains("SECRET"))
        let back = try SettingsCodec.decodePublic(data)
        XCTAssertEqual(back.endpoints[0].token, "")
        XCTAssertEqual(back.imported[0].password, "")
        XCTAssertEqual(back.imported[0].privateKey, "")
        XCTAssertEqual(back.intervalSec, 15)
        XCTAssertEqual(back.activeId, "1")
    }

    func testDashboardHttpTokenAndRedirect() async throws {
        let portFile = FileManager.default.temporaryDirectory.appendingPathComponent("amz-port-\(UUID().uuidString)")
        let script = """
        import sys
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                token = self.headers.get("X-Amnezia-Token", "")
                path = self.path.split("?", 1)[0]
                if token == "bounce":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1/stolen")
                    self.end_headers()
                    return
                if token != "sekret":
                    body = b'{"error":"forbidden"}'
                    self.send_response(403)
                elif path == "/api/health":
                    body = b'{"ok":true,"app":"amnezia-monitor"}'
                    self.send_response(200)
                elif path == "/api/servers":
                    body = b'[{"name":"test1","host":"203.0.113.11","status":"up","cpu":1,"peers_online":1,"peers_total":2}]'
                    self.send_response(200)
                else:
                    body = b'{"error":"not found"}'
                    self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, fmt, *args):
                return
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        open(sys.argv[1], "w").write(str(httpd.server_address[1]))
        httpd.serve_forever()
        """
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        proc.arguments = ["-c", script, portFile.path]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        try proc.run()
        defer { proc.terminate() }
        var port = ""
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline && port.isEmpty {
            if let text = try? String(contentsOf: portFile, encoding: .utf8) {
                port = text.trimmingCharacters(in: .whitespacesAndNewlines)
            }
            if port.isEmpty { try await Task.sleep(nanoseconds: 50_000_000) }
        }
        XCTAssertFalse(port.isEmpty)
        let api = MonitorApi()
        let ep = Endpoint(id: "e", baseUrl: "http://127.0.0.1:\(port)", token: "sekret")
        let health = try await api.health(ep)
        XCTAssertEqual(health.app, "amnezia-monitor")
        XCTAssertTrue(health.ok)
        let servers = try await api.servers(ep)
        XCTAssertEqual(servers.count, 1)
        XCTAssertEqual(servers[0].peersOnline, 1)
        let denied = Endpoint(id: "e", baseUrl: ep.baseUrl, token: "wrong")
        do {
            _ = try await api.health(denied)
            XCTFail("expected 403")
        } catch let error as ApiException {
            XCTAssertEqual(error.code, 403)
            XCTAssertTrue(friendlyError(error).contains("токен"))
        }
        let bounce = Endpoint(id: "e", baseUrl: ep.baseUrl, token: "bounce")
        do {
            _ = try await api.health(bounce)
            XCTFail("expected redirect rejection")
        } catch let error as ApiException {
            XCTAssertTrue(error.message.contains("перенаправила"))
        }
        try? FileManager.default.removeItem(at: portFile)
    }
}

private func qCompress(_ data: Data) -> Data {
    let cap = data.count + data.count / 5 + 64
    var dst = [UInt8](repeating: 0, count: max(cap, 64))
    var outLen = 0
    let dstCap = dst.count
    let rc: Int32 = data.withUnsafeBytes { raw in
        dst.withUnsafeMutableBytes { out in
            amz_zlib_compress(
                raw.bindMemory(to: UInt8.self).baseAddress,
                data.count,
                out.bindMemory(to: UInt8.self).baseAddress,
                dstCap,
                &outLen
            )
        }
    }
    precondition(rc == 0 && outLen > 0)
    var result = Data(count: 4 + outLen)
    let n = data.count
    result[0] = UInt8((n >> 24) & 0xff)
    result[1] = UInt8((n >> 16) & 0xff)
    result[2] = UInt8((n >> 8) & 0xff)
    result[3] = UInt8(n & 0xff)
    result.replaceSubrange(4..<result.count, with: dst.prefix(outLen))
    return result
}

private func b64Url(_ data: Data) -> String {
    var s = data.base64EncodedString()
    s = s.replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
    s = s.replacingOccurrences(of: "=", with: "")
    return s
}
