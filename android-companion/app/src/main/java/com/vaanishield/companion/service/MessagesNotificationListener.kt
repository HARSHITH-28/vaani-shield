package com.vaanishield.companion.service

import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import androidx.work.*
import com.vaanishield.companion.VaaniShieldApp
import com.vaanishield.companion.data.local.QueuedEvent
import com.vaanishield.companion.domain.NotificationParser
import com.vaanishield.companion.worker.UploadWorker
import kotlinx.coroutines.*

class MessagesNotificationListener : NotificationListenerService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val prefs = getSharedPreferences("settings", MODE_PRIVATE)
        if (!prefs.getBoolean("forwarding", false) || prefs.getString("device_id", null) == null) return
        val event = NotificationParser.parse(sbn) ?: return
        scope.launch {
            val dao = (application as VaaniShieldApp).database.events()
            val recent = prefs.getStringSet("submitted_keys", emptySet()).orEmpty()
            if (event.dedupKey in recent) return@launch
            if (dao.count() >= 1000) {
                prefs.edit().putString("last_error", "Local delivery queue is full; forwarding is waiting for space.").apply()
                return@launch
            }
            dao.enqueue(QueuedEvent(event.dedupKey, event.sender, event.senderType, event.text, event.timestamp, event.sourcePackage))
            WorkManager.getInstance(this@MessagesNotificationListener).enqueueUniqueWork("vaanishield-upload", ExistingWorkPolicy.KEEP, OneTimeWorkRequestBuilder<UploadWorker>().build())
        }
    }
    override fun onDestroy() { scope.cancel(); super.onDestroy() }
}
