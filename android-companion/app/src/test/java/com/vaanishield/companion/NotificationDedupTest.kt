package com.vaanishield.companion

import com.vaanishield.companion.domain.NotificationParser
import org.junit.Assert.*
import org.junit.Test

class NotificationDedupTest {
    @Test fun onlyGoogleMessagesIsAccepted() {
        assertTrue(NotificationParser.isGoogleMessages("com.google.android.apps.messaging"))
        assertFalse(NotificationParser.isGoogleMessages("com.whatsapp"))
    }
    @Test fun emptyPreviewsAreRejected() {
        assertFalse(NotificationParser.hasUsablePreview(null))
        assertFalse(NotificationParser.hasUsablePreview("  "))
        assertTrue(NotificationParser.hasUsablePreview("hello"))
    }
    @Test fun retryUsesStableKeyAndRepeatedMessagesRemainDistinctByTimestamp() {
        val firstAttempt = NotificationParser.dedupKey("notif-1", 1000, "unknown", "same")
        val retry = NotificationParser.dedupKey("notif-1", 1000, "unknown", "same")
        val laterLegitimateMessage = NotificationParser.dedupKey("notif-1", 2000, "unknown", "same")
        assertEquals(firstAttempt, retry)
        assertNotEquals(firstAttempt, laterLegitimateMessage)
    }
}
