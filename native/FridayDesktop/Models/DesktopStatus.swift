import Foundation

struct ARLStatus: Decodable {
    var status: String
    var sdkVersion: String?
    var ready: Bool
    var inferenceAttempted: Bool
    var metadataFreshness: String? = nil
    var refreshPending: Bool? = nil
    var connectionState: String? = nil
    var taskReadiness: String? = nil
    var identityVerified: Bool? = nil
    var modelCapabilityVerified: Bool? = nil

    // The legacy ready field represents health metadata, never task permission.
    var connectionPhase: String {
        if refreshPending == true { return "refreshing" }
        if metadataFreshness == "last_known" { return "stale" }
        guard metadataFreshness == "current" || metadataFreshness == "unavailable" else { return "unknown" }
        if status == "ready" && ready && metadataFreshness == "current" { return "connected" }
        switch status {
        case "disconnected": return "disconnected"
        case "not_running": return "not_running"
        case "sdk_missing": return "sdk_missing"
        case "user_disabled": return "disabled"
        case "unsafe_home": return "unsafe_configuration"
        case "starting": return "starting"
        default: return "unknown"
        }
    }

    var connectionDetail: String {
        if connectionPhase == "stale" {
            if status == "ready" && ready { return "ARL 上次健康通信正常；当前连接待核对，身份与模型能力尚未验证。" }
            if status == "disconnected" { return "ARL 上次连接检查失败；当前连接待核对。" }
            return "ARL 仅有上次检查记录；当前连接待核对。"
        }
        if connectionPhase == "refreshing" {
            if metadataFreshness != "last_known" {
                return "ARL 连接状态正在检查，尚无最近确认记录。"
            }
            if status == "ready" && ready {
                return "ARL 上次健康通信正常；正在刷新，身份与模型能力尚未验证。"
            }
            if status == "disconnected" { return "ARL 上次连接检查失败；正在刷新。" }
            return "ARL 正在刷新，保留上次检查结果；当前连接状态待确认。"
        }
        switch connectionPhase {
        case "connected": return "ARL 健康通信正常；身份与模型能力尚未验证。"
        case "disconnected": return "ARL 连接检查失败，健康通信暂不可用。"
        case "not_running": return "ARL 尚未运行。"
        case "sdk_missing": return "本机尚未安装 ARL SDK。"
        case "disabled": return "ARL 已停用。"
        case "unsafe_configuration": return "ARL 本机配置未通过安全检查。"
        case "starting": return "ARL 正在启动，连接就绪状态待确认。"
        default: return "ARL 连接状态尚未确认。"
        }
    }

    var connectionRecovery: String {
        switch connectionPhase {
        case "connected": return "在本次任务预览中核对模型能力与授权。"
        case "disconnected": return "检查本机 ARL 服务后重新读取。"
        case "not_running": return "在本机启动已配置的 ARL 后重新读取。"
        case "sdk_missing": return "按部署指南安装 ARL SDK 后重新读取。"
        case "disabled": return "需要使用时，在 ARL 本机设置中启用后重新读取。"
        case "unsafe_configuration": return "在本机检查 ARL 配置与文件权限后重新读取。"
        case "starting": return "等待启动完成后重新读取。"
        case "refreshing": return "等待本次检查完成后重新读取。"
        case "stale": return "重新读取连接状态；历史记录不能确认当前连接。"
        default: return "重新读取连接状态；若仍不可读，在 Mac 版检查 ARL 服务。"
        }
    }
}

struct HAHTTPRead: Decodable {
    var status: Int
}

struct HAFailureEvidence: Decodable {
    var httpReads: [HAHTTPRead]
}

struct DesktopStatus: Decodable {
    var service: String
    var connection: String
    var failure: String?
    var failureEvidence: HAFailureEvidence?
    var authorizationRevision: String?
    var source: String
    var entityCount: Int
    var deviceWrites: String
    var simulated: Bool
    var pid: Int32
    var configurationAvailable: Bool
    var credentialAvailable: Bool
    var networkHost: String?
    var networkPort: UInt16
    var arl: ARLStatus
    var lifetime: String? = nil
}

enum ServicePhase: String {
    case stopped, starting, running, stopping, failed
}

// Shared presentation consumed by the actual views and the finite native probe.
struct HomeConnectionPresentation {
    let status: DesktopStatus?
    let readable: Bool
    let phase: ServicePhase
    var unknown: Bool { phase == .running && !readable }
    var failure: String? { readable ? status?.failure : nil }
    var retainsConsole: Bool { phase == .running && status?.connection == "connected" }
    var needsAuthorization: Bool { failure == "ha_authorization_rejected" }
    var header: String {
        if unknown { return "连接状态待核对" }
        if status?.connection == "connected" { return "家庭已连接" }
        return needsAuthorization ? "需要授权" : "家庭未连接"
    }
    var headerDetail: String? {
        if unknown { return "历史家庭状态不能证明当前连接。" }
        if let report = status, report.connection == "connected" {
            if report.deviceWrites == "scoped" {
                return "家庭已连接 · 家居按授权控制 · \(report.entityCount) 个设备条目"
            }
            return "请核对连接状态与使用范围。家庭已连接 · 只读 · \(report.entityCount) 个设备条目"
        }
        return needsAuthorization ? "家庭连接需要重新授权" : nil
    }
    var title: String {
        if unknown { return "连接状态待核对" }
        if needsAuthorization { return "需要重新授权 Home Assistant" }
        switch failure {
        case "ha_unreachable": return "无法连接 Home Assistant"
        case "ha_read_timeout": return "读取超时"
        case "subscription_disconnected": return "家庭状态同步已中断"
        case nil: return "连接已有家庭"
        default: return "暂时无法读取家庭状态"
        }
    }
    var titleDetail: String? {
        if needsAuthorization { return "请核对权限与本次操作范围。" }
        return failure == "ha_read_timeout" ? "家庭状态读取超时" : nil
    }
    var detail: String {
        if unknown { return "当前连接状态暂不可读取，历史家庭状态不能证明当前连接。" }
        if needsAuthorization {
            let code = status?.failureEvidence?.httpReads.first(where: { [401, 403].contains($0.status) })?.status
            return code.map { "请核对权限与本次操作范围。Home Assistant 拒绝了现有授权（HTTP \($0)）。" }
                ?? "请核对权限与本次操作范围。当前家庭授权暂不可用。请重新登录并确认授权。"
        }
        switch failure {
        case "ha_unreachable": return "请核对权限与本次操作范围。网关当前无法访问。请检查网关是否在线，以及 Friday 的本地网络权限。"
        case "ha_read_timeout": return "网关没有及时响应。请稍后重新读取。"
        case "subscription_disconnected": return "家庭状态更新出现缺口，历史读数不能代表当前状态。请重新读取；设备操作仍未启用。"
        case nil: return "读取你已配置的家庭网关，查看家庭状态。"
        default: return "请核对连接状态与使用范围。当前连接未通过检查。请重新读取；如果仍失败，可查看本机运行记录。"
        }
    }
}

struct ServiceRequestContext: Equatable {
    let lifecycle: UInt64
    let request: UInt64
}
