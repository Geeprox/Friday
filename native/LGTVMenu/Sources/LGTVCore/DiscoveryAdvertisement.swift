import Foundation
#if canImport(FoundationXML)
import FoundationXML
#endif

public struct DiscoveredTV: Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let host: String
    public init(id: String, name: String, host: String) {
        self.id = id; self.name = name; self.host = host
    }
}

public struct DiscoveryAdvertisement: Sendable {
    public static let service = "urn:lge-com:service:webos-second-screen:1"
    public let id: String
    public let location: URL
    public let host: String

    public init?(response: String, sender: String) {
        guard TVConfiguration.isLANAddress(sender), response.utf8.count < 65536 else { return nil }
        let lines = response.components(separatedBy: "\r\n")
        guard lines.first?.uppercased().hasPrefix("HTTP/1.1 200") == true else { return nil }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let key = line[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            guard headers[key] == nil else { return nil }
            headers[key] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        guard headers["st"]?.lowercased() == Self.service,
              let usn = headers["usn"], usn.lowercased().hasPrefix("uuid:"),
              let raw = headers["location"], let url = URL(string: raw),
              url.scheme == "http", url.host == sender, url.user == nil, url.password == nil,
              url.fragment == nil, url.port.map({ (1...65535).contains($0) }) ?? true else { return nil }
        let uuid = usn.components(separatedBy: "::").first ?? ""
        let normalized = WebOSProtocol.normalizedUUID(uuid)
        guard !normalized.isEmpty else { return nil }
        self.id = normalized; self.location = url; self.host = sender
    }

    public func device(description: Data) -> DiscoveredTV? {
        guard description.count <= 262144 else { return nil }
        let delegate = DescriptionParser()
        let parser = XMLParser(data: description)
        parser.shouldResolveExternalEntities = false
        parser.externalEntityResolvingPolicy = .never
        parser.delegate = delegate
        guard parser.parse(), WebOSProtocol.normalizedUUID(delegate.udn) == id else { return nil }
        let name = delegate.name.trimmingCharacters(in: .whitespacesAndNewlines)
        return DiscoveredTV(id: id, name: name.isEmpty ? "LG webOS TV" : String(name.prefix(120)), host: host)
    }
}

private final class DescriptionParser: NSObject, XMLParserDelegate {
    var name = ""
    var udn = ""
    private var element = ""
    func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?,
                qualifiedName qName: String?, attributes attributeDict: [String: String]) {
        element = elementName
    }
    func parser(_ parser: XMLParser, foundCharacters string: String) {
        if element == "friendlyName" { name += string }
        if element == "UDN" { udn += string }
    }
    func parser(_ parser: XMLParser, didEndElement elementName: String, namespaceURI: String?, qualifiedName qName: String?) {
        element = ""
    }
}
