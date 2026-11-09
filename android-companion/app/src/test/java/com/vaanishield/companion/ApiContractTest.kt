package com.vaanishield.companion

import com.google.gson.Gson
import com.vaanishield.companion.data.remote.IngestRequest
import com.vaanishield.companion.data.remote.PairRequest
import org.junit.Assert.assertEquals
import org.junit.Test

class ApiContractTest {
    @Test fun pairingRequestUsesBackendFieldNames() {
        val json = Gson().toJson(PairRequest("ABCDEFGH", "vs-device", "Phone", "Model", "14", "1.0.0"))
        val map = Gson().fromJson(json, Map::class.java)
        assertEquals(setOf("pairing_code", "device_id", "device_name", "device_model", "android_version", "app_version"), map.keys)
    }
    @Test fun ingestionRequestUsesBackendFieldNamesAndDedupKey() {
        val request = IngestRequest("com.google.android.apps.messaging", "unknown", "unknown", "Preview", "2026-11-07T00:00:00Z", "stable-key")
        val map = Gson().fromJson(Gson().toJson(request), Map::class.java)
        assertEquals(setOf("source_package", "sender", "sender_type", "message_text", "message_timestamp", "client_dedup_key"), map.keys)
        assertEquals("stable-key", map["client_dedup_key"])
    }
}
