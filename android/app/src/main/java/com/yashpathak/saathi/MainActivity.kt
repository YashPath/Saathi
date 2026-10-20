package com.yashpathak.saathi

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.MimeTypeMap
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Chat
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.io.File

private val Ink = Color(0xFF1C2B25)
private val Cream = Color(0xFFFAF6EE)
private val Sand = Color(0xFFF0E6D4)
private val Accent = Color(0xFFA9531A)

private val SaathiColors = lightColorScheme(
    primary = Accent, onPrimary = Color.White, secondary = Ink, background = Cream, surface = Cream,
    surfaceVariant = Sand, onSurface = Ink, onBackground = Ink,
)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val store = Store(this)
        val api = Api(store)
        setContent {
            MaterialTheme(colorScheme = SaathiColors) {
                var loggedIn by remember { mutableStateOf(store.token != null) }
                if (loggedIn) Home(store, api) { loggedIn = false } else AuthScreen(store, api) { loggedIn = true }
            }
        }
    }
}

// --- login and sign-up --------------------------------------------------------------

@Composable
fun AuthScreen(store: Store, api: Api, onDone: () -> Unit) {
    val scope = rememberCoroutineScope()
    var signUp by remember { mutableStateOf(false) }
    var server by remember { mutableStateOf(store.server) }
    var phone by remember { mutableStateOf("") }
    var pin by remember { mutableStateOf("") }
    var name by remember { mutableStateOf("") }
    var role by remember { mutableStateOf("shopkeeper") }
    var code by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    Scaffold { pad ->
        Column(
            Modifier.padding(pad).padding(24.dp).fillMaxSize().verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Saathi", style = MaterialTheme.typography.displaySmall, fontWeight = FontWeight.SemiBold, color = Ink)
            Text("Your assistant for the shop and the classroom. Speak or type in Hindi, English or Hinglish.",
                color = Ink.copy(alpha = 0.7f))
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(server, { server = it }, label = { Text("Server address") },
                placeholder = { Text("https://xxxx.ngrok-free.app") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(!signUp, { signUp = false }, label = { Text("Log in") })
                FilterChip(signUp, { signUp = true }, label = { Text("Create account") })
            }
            if (signUp) {
                Text("I am a", fontWeight = FontWeight.Medium)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("shopkeeper" to "Shopkeeper", "professor" to "Professor", "student" to "Student").forEach { (k, v) ->
                        FilterChip(role == k, { role = k }, label = { Text(v) })
                    }
                }
                if (role == "student") {
                    OutlinedTextField(code, { code = it.filter(Char::isDigit).take(6) }, label = { Text("Setup code from your professor") },
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword), singleLine = true,
                        modifier = Modifier.fillMaxWidth())
                } else {
                    OutlinedTextField(name, { name = it }, label = { Text(if (role == "shopkeeper") "Shop name" else "Your name") },
                        singleLine = true, modifier = Modifier.fillMaxWidth())
                }
            }
            OutlinedTextField(phone, { phone = it }, label = { Text("Mobile number") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone), modifier = Modifier.fillMaxWidth())
            OutlinedTextField(pin, { pin = it.filter(Char::isDigit).take(6) }, label = { Text(if (signUp) "Choose a 6-digit PIN" else "6-digit PIN") },
                singleLine = true, visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword), modifier = Modifier.fillMaxWidth())
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            Button(
                enabled = !busy,
                onClick = {
                    store.server = server
                    busy = true; error = null
                    scope.launch {
                        try {
                            if (signUp) api.register(phone, pin, role, name, code) else api.login(phone, pin)
                            onDone()
                        } catch (e: ApiError) {
                            error = e.message
                        } finally {
                            busy = false
                        }
                    }
                },
                modifier = Modifier.fillMaxWidth().height(52.dp),
            ) { Text(if (busy) "Please wait…" else if (signUp) "Create account" else "Log in") }
        }
    }
}

// --- home with tabs --------------------------------------------------------------------

@Composable
fun Home(store: Store, api: Api, onSignedOut: () -> Unit) {
    var tab by rememberSaveable { mutableIntStateOf(0) }
    val signOut = { store.signOut(); onSignedOut() }
    Scaffold(
        bottomBar = {
            NavigationBar(containerColor = Sand) {
                NavigationBarItem(tab == 0, { tab = 0 }, { Icon(Icons.AutoMirrored.Filled.Chat, null) }, label = { Text("Chat") })
                NavigationBarItem(tab == 1, { tab = 1 }, { Icon(Icons.Default.Insights, null) }, label = { Text("Dashboard") })
                NavigationBarItem(tab == 2, { tab = 2 }, { Icon(Icons.Default.Person, null) }, label = { Text("Account") })
            }
        },
    ) { pad ->
        Box(Modifier.padding(pad).consumeWindowInsets(pad)) {
            when (tab) {
                0 -> ChatScreen(store, api, signOut)
                1 -> DashboardScreen(store)
                else -> AccountScreen(store, api, signOut)
            }
        }
    }
}

// --- chat ----------------------------------------------------------------------------------

sealed class Item {
    data class Me(val text: String) : Item()
    data class Bot(val text: String, val label: String? = null) : Item()
    data class Attachment(val file: FileRef) : Item()
    data class Outgoing(val share: Share) : Item()
}

@Composable
fun ChatScreen(store: Store, api: Api, onExpired: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val items = remember { mutableStateListOf<Item>() }
    var quick by remember { mutableStateOf(listOf<String>()) }
    var input by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val recorder = remember { VoiceRecorder(context) }
    var recording by remember { mutableStateOf(false) }
    val list = rememberLazyListState()

    fun fail(e: ApiError) {
        if (e.expired) onExpired() else items.add(Item.Bot(e.message ?: "Something went wrong.", "Error"))
    }

    fun show(turn: Turn) {
        turn.heard?.let { items.add(Item.Me("🎙 $it")) }
        items.add(Item.Bot(turn.reply))
        turn.messages.forEach { items.add(Item.Bot(it)) }
        turn.files.forEach { items.add(Item.Attachment(it)) }
        turn.shares.forEach { items.add(Item.Outgoing(it)) }
        quick = turn.quickReplies
    }

    fun send(text: String) {
        if (text.isBlank() || busy) return
        items.add(Item.Me(text)); input = ""; quick = emptyList(); busy = true
        scope.launch {
            try { show(api.chat(text)) } catch (e: ApiError) { fail(e) } finally { busy = false }
        }
    }

    val micPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        if (!ok) Toast.makeText(context, "Microphone permission is needed for voice notes.", Toast.LENGTH_LONG).show()
    }

    fun toggleRecording() {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            micPermission.launch(Manifest.permission.RECORD_AUDIO); return
        }
        if (!recording) {
            recording = recorder.start()
            if (!recording) Toast.makeText(context, "Couldn't start the microphone.", Toast.LENGTH_SHORT).show()
            return
        }
        recording = false
        val file = recorder.stop() ?: return
        busy = true; quick = emptyList()
        scope.launch {
            try { show(api.voice(file)) } catch (e: ApiError) { fail(e) } finally { busy = false; file.delete() }
        }
    }

    LaunchedEffect(Unit) {
        try {
            api.history().forEach { (role, text) -> items.add(if (role == "user") Item.Me(text) else Item.Bot(text)) }
            if (items.isEmpty()) items.add(Item.Bot(greeting(store.role)))
        } catch (e: ApiError) { fail(e) }
        while (true) { // new messages from other people and the evening jobs
            try {
                api.inbox().forEach { n ->
                    n.text?.let { items.add(Item.Bot(it, "New")) }
                    n.filename?.let { f -> items.add(Item.Attachment(FileRef(n.id, f))) }
                }
            } catch (e: ApiError) { if (e.expired) { onExpired(); break } }
            delay(20_000)
        }
    }
    LaunchedEffect(items.size) { if (items.isNotEmpty()) list.animateScrollToItem(items.size - 1) }
    DisposableEffect(Unit) { onDispose { recorder.cancel() } }

    Column(Modifier.fillMaxSize().imePadding()) {
        LazyColumn(Modifier.weight(1f).fillMaxWidth(), state = list, contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(items) { item ->
                when (item) {
                    is Item.Me -> Bubble(item.text, mine = true)
                    is Item.Bot -> Bubble(item.text, mine = false, label = item.label)
                    is Item.Attachment -> FileCard(item.file) { scope.launch { openFile(context, api, item.file) } }
                    is Item.Outgoing -> ShareCard(item.share, onWhatsApp = { whatsApp(context, item.share) },
                        onSms = { sms(context, item.share) },
                        onFile = item.share.fileId?.let { id -> fun() { scope.launch { openFile(context, api, FileRef(id, "upi_qr.png")) } } })
                }
            }
            if (busy) item { Text("Saathi is working…", color = Ink.copy(alpha = 0.6f), modifier = Modifier.padding(8.dp)) }
        }
        if (quick.isNotEmpty()) {
            Row(Modifier.padding(horizontal = 12.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                quick.forEach { q -> AssistChip(onClick = { send(q) }, label = { Text(q) }) }
            }
        }
        Row(Modifier.fillMaxWidth().padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(input, { input = it }, Modifier.weight(1f),
                placeholder = { Text(if (recording) "Recording… tap the mic to send" else "Type or tap the mic") },
                maxLines = 4, enabled = !recording)
            IconButton(onClick = { toggleRecording() }, enabled = !busy) {
                Icon(if (recording) Icons.Default.Stop else Icons.Default.Mic,
                    contentDescription = if (recording) "Stop and send voice note" else "Record a voice note",
                    tint = if (recording) MaterialTheme.colorScheme.error else Accent)
            }
            IconButton(onClick = { send(input.trim()) }, enabled = !busy && input.isNotBlank()) {
                Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send", tint = Accent)
            }
        }
    }
}

fun greeting(role: String) = when (role) {
    "shopkeeper" -> "Namaste! Try: \"Sharma ji ka 500 udhaar likh do\", \"2 maggi ka bill banao\" or \"aaj ka hisaab\"."
    "professor" -> "Namaste! Try: \"CSE-3 class banao\", \"CSE-3 attendance: 2 aur 4 absent\" or \"quiz banao\"."
    else -> "Namaste! Say \"join CSE-3\" to accept your class invite, then ask \"meri attendance?\" or any doubt."
}

@Composable
fun Bubble(text: String, mine: Boolean, label: String? = null) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (mine) Arrangement.End else Arrangement.Start) {
        Column(
            Modifier.widthIn(max = 300.dp)
                .background(if (mine) Ink else Sand, RoundedCornerShape(16.dp))
                .padding(horizontal = 14.dp, vertical = 10.dp),
        ) {
            label?.let { Text(it, color = Accent, fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.labelSmall) }
            Text(text, color = if (mine) Cream else Ink)
        }
    }
}

@Composable
fun FileCard(file: FileRef, onOpen: () -> Unit) {
    OutlinedCard(onClick = onOpen, modifier = Modifier.fillMaxWidth(0.85f)) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Default.Description, null, tint = Accent)
            Spacer(Modifier.width(10.dp))
            Column {
                Text(file.filename, fontWeight = FontWeight.Medium)
                Text("Tap to open", style = MaterialTheme.typography.bodySmall, color = Ink.copy(alpha = 0.6f))
            }
        }
    }
}

@Composable
fun ShareCard(share: Share, onWhatsApp: () -> Unit, onSms: () -> Unit, onFile: (() -> Unit)?) {
    OutlinedCard(Modifier.fillMaxWidth(0.92f)) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("Ready to send to +${share.to}", fontWeight = FontWeight.SemiBold, color = Accent)
            if (share.text.isNotBlank()) Text(share.text)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onWhatsApp) { Text("WhatsApp") }
                OutlinedButton(onClick = onSms) { Text("SMS") }
                onFile?.let { OutlinedButton(onClick = it) { Text("QR") } }
            }
        }
    }
}

fun whatsApp(context: Context, share: Share) =
    startSafely(context, Intent(Intent.ACTION_VIEW, Uri.parse("https://wa.me/${share.to}?text=${Uri.encode(share.text)}")))

fun sms(context: Context, share: Share) =
    startSafely(context, Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:+${share.to}")).putExtra("sms_body", share.text))

private fun startSafely(context: Context, intent: Intent) {
    try { context.startActivity(intent) } catch (e: Exception) {
        Toast.makeText(context, "No app found to send this.", Toast.LENGTH_SHORT).show()
    }
}

suspend fun openFile(context: Context, api: Api, file: FileRef) {
    try {
        val dir = File(context.cacheDir, "downloads").apply { mkdirs() }
        val local = api.download(file.id, file.filename, dir)
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", local)
        val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(local.extension.lowercase()) ?: "*/*"
        val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        context.startActivity(Intent.createChooser(view, file.filename))
    } catch (e: ApiError) {
        Toast.makeText(context, e.message, Toast.LENGTH_SHORT).show()
    } catch (e: Exception) {
        Toast.makeText(context, "No app found to open ${file.filename}.", Toast.LENGTH_SHORT).show()
    }
}

// --- voice --------------------------------------------------------------------------------

/** Records AAC audio into the cache; Gemini accepts audio/aac. */
class VoiceRecorder(private val context: Context) {
    private var recorder: MediaRecorder? = null
    private var file: File? = null

    fun start(): Boolean = try {
        val out = File(context.cacheDir, "voice_${System.currentTimeMillis()}.aac")
        val r = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) MediaRecorder(context) else @Suppress("DEPRECATION") MediaRecorder()
        r.setAudioSource(MediaRecorder.AudioSource.MIC)
        r.setOutputFormat(MediaRecorder.OutputFormat.AAC_ADTS)
        r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
        r.setAudioSamplingRate(16_000)
        r.setAudioEncodingBitRate(32_000)
        r.setOutputFile(out.absolutePath)
        r.prepare()
        r.start()
        recorder = r; file = out
        true
    } catch (e: Exception) {
        cancel(); false
    }

    /** Stop and return the recording, or null if it was too short or failed. */
    fun stop(): File? {
        val r = recorder ?: return null
        val out = file
        recorder = null; file = null
        return try { r.stop(); r.release(); out?.takeIf { it.length() > 0 } } catch (e: Exception) { r.release(); out?.delete(); null }
    }

    fun cancel() {
        runCatching { recorder?.release() }
        recorder = null; file?.delete(); file = null
    }
}

// --- dashboard and account -------------------------------------------------------------------

@SuppressLint("SetJavaScriptEnabled")
@Composable
fun DashboardScreen(store: Store) {
    AndroidView(
        modifier = Modifier.fillMaxSize(),
        factory = { ctx ->
            WebView(ctx).apply {
                settings.javaScriptEnabled = true
                settings.domStorageEnabled = true // the page keeps its token in sessionStorage
                webViewClient = WebViewClient()
                loadUrl("${store.server}/dashboard#t=${store.token}")
            }
        },
    )
}

@Composable
fun AccountScreen(store: Store, api: Api, onSignedOut: () -> Unit) {
    val scope = rememberCoroutineScope()
    Column(Modifier.padding(24.dp).fillMaxSize(), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(store.name, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold)
        Text(store.role.replaceFirstChar { it.uppercase() }, color = Accent)
        Text("Server: ${store.server}", color = Ink.copy(alpha = 0.7f))
        Spacer(Modifier.height(12.dp))
        Button(onClick = { scope.launch { api.logout(); onSignedOut() } }) { Text("Log out on all phones") }
        Text("Logging out also stops every dashboard link sent so far.", style = MaterialTheme.typography.bodySmall,
            color = Ink.copy(alpha = 0.6f))
    }
}
