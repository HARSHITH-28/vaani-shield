package com.vaanishield.companion.worker

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkerParameters
import com.vaanishield.companion.VaaniShieldApp
import com.vaanishield.companion.data.remote.*
import com.vaanishield.companion.security.TokenVault
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory

class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val prefs = applicationContext.getSharedPreferences("settings", Context.MODE_PRIVATE)
        if (!prefs.getBoolean("forwarding", false)) return Result.success()
        val token = TokenVault(applicationContext).read() ?: return Result.failure()
        val url = (prefs.getString("server_url", "") ?: "").trimEnd('/') + "/"
        val api = runCatching { Retrofit.Builder().baseUrl(url).addConverterFactory(GsonConverterFactory.create()).build().create(VaaniApi::class.java) }.getOrElse { return Result.failure() }
        val dao = (applicationContext as VaaniShieldApp).database.events()
        return try {
            val heartbeat = api.heartbeat("Bearer $token", HeartbeatRequest("1.0.0"))
            if (heartbeat.code() == 401 || heartbeat.code() == 403) {
                prefs.edit().putString("last_error", "Device authorization is no longer active (HTTP ${heartbeat.code()}).").apply()
                return Result.failure()
            }
            if (heartbeat.code() == 429 || heartbeat.code() >= 500) return Result.retry()
            if (!heartbeat.isSuccessful || heartbeat.body()?.forwarding_permitted != true) return Result.failure()
            prefs.edit().putString("last_sync", heartbeat.body()?.last_seen_at ?: "Connected").apply()
            repeat(20) {
                if (!prefs.getBoolean("forwarding", false)) return Result.success()
                val item = dao.oldest() ?: return Result.success()
                val response = api.ingest("Bearer $token", IngestRequest(item.sourcePackage, item.sender, item.senderType, item.text, item.timestamp, item.dedupKey))
                when {
                    response.isSuccessful -> {
                        dao.remove(item.dedupKey)
                        prefs.edit().putInt("processed_count", prefs.getInt("processed_count", 0) + 1).apply()
                        val keys = prefs.getStringSet("submitted_keys", emptySet()).orEmpty().toMutableSet()
                        keys.add(item.dedupKey)
                        prefs.edit().putStringSet("submitted_keys", keys.toList().takeLast(500).toSet()).apply()
                    }
                    response.code() in listOf(401, 403, 422) -> { prefs.edit().putString("last_error", "Delivery rejected: HTTP ${response.code()}").apply(); return Result.failure() }
                    response.code() == 429 || response.code() >= 500 -> return Result.retry()
                    else -> { prefs.edit().putString("last_error", "Delivery failed: HTTP ${response.code()}").apply(); return Result.failure() }
                }
            }
            if (dao.count() > 0) androidx.work.WorkManager.getInstance(applicationContext).enqueue(OneTimeWorkRequestBuilder<UploadWorker>().build())
            Result.success()
        } catch (_: Exception) { Result.retry() }
    }
}
