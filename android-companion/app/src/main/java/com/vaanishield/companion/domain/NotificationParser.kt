package com.vaanishield.companion.domain

import android.app.Notification
import android.app.Person
import android.os.Bundle
import android.service.notification.StatusBarNotification
import java.security.MessageDigest
import java.time.Instant

data class MessagePreview(val sender: String, val senderType: String, val text: String, val timestamp: String, val dedupKey: String, val sourcePackage: String)

object NotificationParser {
    const val GOOGLE_MESSAGES = "com.google.android.apps.messaging"
    fun isGoogleMessages(packageName: String): Boolean = packageName == GOOGLE_MESSAGES
    fun hasUsablePreview(text: String?): Boolean = !text.isNullOrBlank()
    fun dedupKey(notificationKey: String, timestamp: Long, sender: String, text: String): String {
        val stable = "$notificationKey|$timestamp|$sender|$text"
        return MessageDigest.getInstance("SHA-256").digest(stable.toByteArray()).joinToString("") { "%02x".format(it) }
    }
    fun parse(sbn: StatusBarNotification): MessagePreview? {
        if (!isGoogleMessages(sbn.packageName)) return null
        val n = sbn.notification
        val extras = n.extras ?: Bundle.EMPTY
        var sender: String? = null
        var content: CharSequence? = null
        var timestamp = sbn.postTime
        val messages = extras.getParcelableArray(Notification.EXTRA_MESSAGES)
        if (!messages.isNullOrEmpty()) {
            val latest = messages.lastOrNull() as? Bundle
            if (latest != null) {
                content = latest.getCharSequence("text")
                timestamp = latest.getLong("time", sbn.postTime)
                @Suppress("DEPRECATION")
                val person = (latest.getParcelable("sender_person") as? Person) ?: (latest.getParcelable("sender") as? Person)
                val legacyName = latest.getCharSequence("sender")?.toString()
                sender = person?.name?.toString()?.takeIf { it.isNotBlank() } ?: legacyName?.takeIf { it.isNotBlank() }
            }
        }
        if (content.isNullOrBlank()) content = extras.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: extras.getCharSequence(Notification.EXTRA_TEXT)
        val text = content?.toString()?.trim().orEmpty()
        if (!hasUsablePreview(text)) return null
        val safeSender = sender?.trim()?.take(120)?.takeIf { it.isNotBlank() } ?: "unknown"
        val senderType = if (safeSender == "unknown") "unknown" else "contact"
        val key = dedupKey(sbn.key, timestamp, safeSender, text)
        return MessagePreview(safeSender, senderType, text, Instant.ofEpochMilli(timestamp).toString(), key, sbn.packageName)
    }
}
