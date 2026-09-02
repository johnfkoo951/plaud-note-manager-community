import Foundation

struct PlaudWebAuthCapture: Codable, Equatable {
    var authorization: String
    var xDeviceID: String
    /// Legacy request header. New Plaud Web API traffic may omit it.
    var xPldUser: String?
    var cookie: String?
    var xPldTag: String?
    var baseURL: String?
    var appLanguage: String?
    var appPlatform: String?
    var editFrom: String?
    var origin: String?
    var referer: String?
    var timezone: String?
    /// Raw web.plaud.ai localStorage `workspaceList` JSON. Carries the
    /// workspace *refresh* token, which the CLI stores so it can renew the
    /// 24h token headlessly — the reason embedded login beats cURL import.
    var workspaceList: String?

    enum CodingKeys: String, CodingKey {
        case authorization
        case xDeviceID = "x_device_id"
        case xPldUser = "x_pld_user"
        case cookie
        case xPldTag = "x_pld_tag"
        case baseURL = "base_url"
        case appLanguage = "app_language"
        case appPlatform = "app_platform"
        case editFrom = "edit_from"
        case origin
        case referer
        case timezone
        case workspaceList = "workspace_list"
    }
}
