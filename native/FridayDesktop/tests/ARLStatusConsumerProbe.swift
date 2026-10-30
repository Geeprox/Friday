import Foundation

// Compile with the exact DesktopStatus.swift. No App, SDK, network or child runs.
@main
struct ARLStatusConsumerProbe {
    static func expect(_ value: Bool, _ reason: String) {
        precondition(value, reason)
    }
    static func decode(_ value: [String: Any]) throws -> ARLStatus {
        let data = try JSONSerialization.data(withJSONObject: value)
        return try JSONDecoder().decode(ARLStatus.self, from: data)
    }
    static func main() throws {
        if CommandLine.arguments.contains("--project") {
            let values = try JSONDecoder().decode([ARLStatus].self, from: FileHandle.standardInput.readDataToEndOfFile())
            let output = values.map { ["state": $0.connectionPhase, "description": $0.connectionDetail, "recovery": $0.connectionRecovery] }
            FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]))
            return
        }
        let base: [String: Any] = ["status": "ready", "sdkVersion": "synthetic", "ready": true, "inferenceAttempted": false]
        let legacy = try decode(base)
        expect(legacy.metadataFreshness == nil && legacy.refreshPending == nil, "Legacy public metadata still decodes")
        expect(legacy.connectionPhase == "unknown", "Legacy ready has no current freshness evidence")
        expect(legacy.connectionDetail == "ARL 连接状态尚未确认。", "Legacy readiness cannot qualify a current connection")
        var fresh = base
        fresh["metadataFreshness"] = "current"; fresh["refreshPending"] = false
        fresh["connectionState"] = "connected"; fresh["taskReadiness"] = "unverified"
        fresh["identityVerified"] = false; fresh["modelCapabilityVerified"] = false
        let current = try decode(fresh)
        expect(current.connectionPhase == "connected", "Fresh health qualifies connection only")
        expect(current.connectionDetail == "ARL 健康通信正常；身份与模型能力尚未验证。", "Current health does not qualify a model task")
        expect(current.identityVerified == false && current.modelCapabilityVerified == false, "Backend task verification remains explicit")
        var warm = base
        warm["metadataFreshness"] = "last_known"; warm["refreshPending"] = true
        let cachedReady = try decode(warm)
        expect(cachedReady.connectionDetail.contains("上次健康通信正常"), "Refresh retains previous successful connection")
        expect(cachedReady.connectionPhase == "refreshing", "Cached readiness is not a current connected state")
        warm["refreshPending"] = false
        let stale = try decode(warm)
        expect(stale.connectionPhase == "stale" && !stale.connectionDetail.contains("正在"), "Historical metadata without a pending check cannot claim refreshing")
        var cold = base
        cold["ready"] = false; cold["status"] = "runtime_busy"
        cold["metadataFreshness"] = "unknown"; cold["refreshPending"] = true
        let unknown = try decode(cold)
        expect(unknown.connectionDetail.contains("正在检查") && !unknown.connectionDetail.contains("不可用"), "Cold local busy is pending, not disconnected")
        for code in ["deadline_exceeded", "discarded_configuration_changed"] {
            cold["status"] = code
            let pending = try decode(cold)
            expect(pending.connectionDetail == unknown.connectionDetail, "Timeout or old configuration cannot imply disconnection")
        }
        var failure = cold
        failure["status"] = "disconnected"; failure["metadataFreshness"] = "unavailable"; failure["refreshPending"] = false
        let disconnected = try decode(failure)
        expect(disconnected.connectionDetail.contains("连接检查失败"), "Confirmed disconnection may report unavailable")
        failure["metadataFreshness"] = "last_known"; failure["refreshPending"] = true
        let cachedFailure = try decode(failure)
        expect(cachedFailure.connectionDetail == "ARL 上次连接检查失败；正在刷新。", "Cached failure remains historical during refresh")
        var starting = base
        starting["ready"] = false; starting["status"] = "starting"; starting["metadataFreshness"] = "current"
        let startingState = try decode(starting)
        expect(startingState.connectionDetail.contains("正在启动"), "Starting is not a failed connection")
        var conflict = fresh
        conflict["status"] = "user_disabled"
        let disabled = try decode(conflict)
        expect(disabled.connectionPhase == "disabled" && disabled.connectionDetail.contains("已停用"), "Disabled status dominates contradictory ready")
        conflict["status"] = "ready"; conflict["metadataFreshness"] = "unknown"
        expect(try decode(conflict).connectionPhase == "unknown", "Unknown freshness cannot reuse ready")
        print("ARLStatusConsumerProbe: 12 finite synthetic presentation cases passed")
    }
}
