package com.vaanishield.companion.data.remote

import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.Header
import retrofit2.http.POST

data class PairRequest(val pairing_code: String, val device_id: String, val device_name: String, val device_model: String, val android_version: String, val app_version: String)
data class PairResponse(val device_id: String, val status: String, val device_token: String)
data class HeartbeatRequest(val app_version: String)
data class HeartbeatResponse(val status: String, val forwarding_permitted: Boolean, val last_seen_at: String, val server_time: String)
data class IngestRequest(val source_package: String, val sender: String, val sender_type: String, val message_text: String, val message_timestamp: String, val client_dedup_key: String)
data class IngestResponse(val sms_event_id: String, val deduplicated: Boolean, val risk_level: String)

interface VaaniApi {
    @POST("api/v1/devices/pair") suspend fun pair(@Body body: PairRequest): Response<PairResponse>
    @POST("api/v1/devices/heartbeat") suspend fun heartbeat(@Header("Authorization") authorization: String, @Body body: HeartbeatRequest): Response<HeartbeatResponse>
    @POST("api/v1/devices/sms/ingest") suspend fun ingest(@Header("Authorization") authorization: String, @Body body: IngestRequest): Response<IngestResponse>
}
