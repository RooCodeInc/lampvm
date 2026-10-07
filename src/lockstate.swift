// Prints whether the host's screen is locked, for `lampvm status`.
import CoreGraphics
let session = CGSessionCopyCurrentDictionary() as? [String: Any] ?? [:]
print((session["CGSSessionScreenIsLocked"] as? Bool) == true ? "locked" : "unlocked")
