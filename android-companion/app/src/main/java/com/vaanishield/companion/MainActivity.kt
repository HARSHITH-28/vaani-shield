package com.vaanishield.companion

import android.content.ComponentName
import android.content.Intent
import android.os.Build
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.app.NotificationManager
import android.content.Context
import android.net.Uri
import android.os.Bundle
import android.provider.Settings.Secure
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.lifecycleScope
import androidx.work.*
import com.vaanishield.companion.data.remote.*
import com.vaanishield.companion.security.TokenVault
import com.vaanishield.companion.worker.UploadWorker
import kotlinx.coroutines.launch
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { CompanionScreen() }
    }
}

@Composable private fun CompanionScreen() {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("settings", Context.MODE_PRIVATE) }
    var server by remember { mutableStateOf(prefs.getString("server_url", "") ?: "") }
    var code by remember { mutableStateOf("") }
    var message by remember { mutableStateOf("Not paired") }
    var forwarding by remember { mutableStateOf(prefs.getBoolean("forwarding", false)) }
    var paired by remember { mutableStateOf(TokenVault(context).read() != null) }
    var diagnostics by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val notificationAccess = remember { mutableStateOf(hasNotificationAccess(context)) }
    val queued = remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) { queued.intValue = (context.applicationContext as VaaniShieldApp).database.events().count() }
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                notificationAccess.value = hasNotificationAccess(context)
                scope.launch { queued.intValue = (context.applicationContext as VaaniShieldApp).database.events().count() }
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
    MaterialTheme(colorScheme = lightColorScheme(primary = androidx.compose.ui.graphics.Color(0xFF176B55))) {
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("VaaniShield", style = MaterialTheme.typography.headlineMedium)
            Text("Android companion · Notification previews")
            Text("Backend connection: ${if (prefs.getString("last_sync", null) == null) "Not yet confirmed" else "Connected at ${prefs.getString("last_sync", "")}"}")
            Text("Notification access: ${if (notificationAccess.value) "Allowed" else "Unavailable"}")
            Text("Pairing: ${if (paired) "Paired" else "Not paired"}")
            Text("Forwarding: ${if (forwarding) "Active" else "Paused"}")
            Text("Pending messages: ${queued.intValue}")
            Text("Messages delivered: ${prefs.getInt("processed_count", 0)}")
            Text("Last sync: ${prefs.getString("last_sync", "Not yet synchronized")}")
            prefs.getString("last_error", null)?.let { Text("Recent sync error: $it", color = MaterialTheme.colorScheme.error) }
            OutlinedTextField(server, { server = it }, label = { Text("Backend URL") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Text("HTTPS is required in production. HTTP is permitted only for a trusted private LAN during development.", color = MaterialTheme.colorScheme.error)
            OutlinedTextField(code, { code = it.uppercase().take(8) }, label = { Text("8-character pairing code") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Button(onClick = {
                val cleanUrl = server.trim().trimEnd('/')
                val parsed = runCatching { Uri.parse(cleanUrl) }.getOrNull()
                if (parsed == null || parsed.host.isNullOrBlank() || (parsed.scheme != "https" && !(BuildConfig.DEBUG && parsed.scheme == "http" && isPrivateLanHost(parsed.host!!)))) {
                    message = "Use HTTPS, or an HTTP private-LAN address (localhost or private IP)."; return@Button
                }
                if (code.length != 8) { message = "Enter the eight-character pairing code."; return@Button }
                prefs.edit().putString("server_url", cleanUrl).apply()
                scope.launch {
                    message = "Pairing…"
                    runCatching {
                        val api = apiFor(cleanUrl)
                        val androidId = Secure.getString(context.contentResolver, Secure.ANDROID_ID) ?: "device-${System.currentTimeMillis()}"
                        val req = PairRequest(code, "vs-$androidId", Build.MODEL.take(120), Build.MODEL.take(120), Build.VERSION.RELEASE.take(32), "1.0.0")
                        val response = api.pair(req)
                        if (!response.isSuccessful) error("Pairing failed: HTTP ${response.code()}")
                        val result = response.body() ?: error("The server returned no pairing result.")
                        TokenVault(context).store(result.device_token)
                        prefs.edit().putString("device_id", result.device_id).apply()
                        val periodic = PeriodicWorkRequestBuilder<UploadWorker>(15, java.util.concurrent.TimeUnit.MINUTES)
                            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build()
                        WorkManager.getInstance(context).enqueueUniquePeriodicWork("vaanishield-heartbeat", ExistingPeriodicWorkPolicy.UPDATE, periodic)
                        paired = true; code = ""; message = "Paired successfully"
                    }.onFailure { message = it.message ?: "Pairing failed" }
                }
            }, modifier = Modifier.fillMaxWidth()) { Text("Pair device") }
            Button(onClick = { context.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }, modifier = Modifier.fillMaxWidth()) { Text("Open Notification Access settings") }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("Enable forwarding")
                Switch(checked = forwarding, onCheckedChange = { enabled ->
                    forwarding = enabled
                    prefs.edit().putBoolean("forwarding", enabled).apply()
                    if (enabled && paired) WorkManager.getInstance(context).enqueue(OneTimeWorkRequestBuilder<UploadWorker>().build())
                    else WorkManager.getInstance(context).cancelUniqueWork("vaanishield-upload")
                }, enabled = paired)
            }
            if (server.startsWith("http://")) Text("Development HTTP is active. Message previews and device token are unencrypted in transit on this LAN.", color = MaterialTheme.colorScheme.error)
            Button(onClick = { diagnostics = !diagnostics }) { Text("${if (diagnostics) "Hide" else "View"} diagnostics") }
            if (diagnostics) Text("$message\nPending items: ${queued.intValue}\nPreview capture is limited to visible Google Messages notifications; previews can be hidden, incomplete, grouped, or suppressed while a conversation is open. Device background limits can delay delivery.")
            OutlinedButton(onClick = {
                forwarding = false; paired = false; prefs.edit().putBoolean("forwarding", false).remove("device_id").apply(); TokenVault(context).clear(); message = "Local credentials cleared; revoke this device from the website to unpair it server-side."
                WorkManager.getInstance(context).cancelUniqueWork("vaanishield-heartbeat")
            }, modifier = Modifier.fillMaxWidth()) { Text("Clear local credentials") }
            Text(message)
            Text("Only Google Messages previews are eligible. Content is sent to the configured VaaniShield backend for scam analysis after you pair and enable forwarding.", style = MaterialTheme.typography.bodySmall)
        }
    }
}

private fun apiFor(url: String): VaaniApi = Retrofit.Builder().baseUrl(url.trimEnd('/') + "/").addConverterFactory(GsonConverterFactory.create()).build().create(VaaniApi::class.java)
private fun isPrivateLanHost(host: String): Boolean {
    if (host == "localhost" || host == "10.0.2.2") return true
    val octets = host.split('.').mapNotNull { it.toIntOrNull() }
    if (octets.size != 4 || octets.any { it !in 0..255 }) return false
    return octets[0] == 10 || (octets[0] == 192 && octets[1] == 168) || (octets[0] == 172 && octets[1] in 16..31)
}
private fun hasNotificationAccess(context: Context): Boolean = runCatching {
    val enabled = Settings.Secure.getString(context.contentResolver, "enabled_notification_listeners") ?: return false
    enabled.contains(context.packageName)
}.getOrDefault(false)
