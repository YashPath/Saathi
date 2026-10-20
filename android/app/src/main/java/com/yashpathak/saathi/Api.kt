package com.yashpathak.saathi

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Login state kept on the phone: the server address, the login token and who is logged in. */
class Store(context: Context) {
    private val prefs = context.getSharedPreferences("saathi", Context.MODE_PRIVATE)

    var server: String
        get() = prefs.getString("server", "") ?: ""
        set(v) = prefs.edit().putString("server", v.trim().trimEnd('/')).apply()
    var token: String?
        get() = prefs.getString("token", null)
        set(v) = prefs.edit().putString("token", v).apply()
    var name: String
        get() = prefs.getString("name", "") ?: ""
        set(v) = prefs.edit().putString("name", v).apply()
    var role: String
        get() = prefs.getString("role", "") ?: ""
        set(v) = prefs.edit().putString("role", v).apply()

    fun signOut() = prefs.edit().remove("token").remove("name").remove("role").apply()
}

/** An error worth showing to the user as it is. `expired` = the login no longer works. */
class ApiError(message: String, val expired: Boolean = false) : Exception(message)

data class FileRef(val id: Int, val filename: String)
data class Share(val to: String, val text: String, val fileId: Int?)
data class Turn(
    val heard: String?, val reply: String, val messages: List<String>, val files: List<FileRef>,
    val shares: List<Share>, val quickReplies: List<String>,
)
data class InboxItem(val id: Int, val text: String?, val filename: String?)

class Api(private val store: Store) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS) // the agent may call several tools before replying
        .build()
    private val json = "application/json".toMediaType()

    private fun url(path: String): String {
        val base = store.server
        if (!base.startsWith("https://") && !base.startsWith("http://")) throw ApiError("Set the server address first.")
        return base + path
    }

    private suspend fun call(request: Request): JSONObject = withContext(Dispatchers.IO) {
        try {
            http.newCall(request).execute().use { r ->
                val body = r.body?.string().orEmpty()
                val obj = runCatching { JSONObject(body) }.getOrNull() ?: JSONObject()
                if (!r.isSuccessful) {
                    val detail = obj.optString("detail").ifEmpty { "Server error (${r.code})." }
                    throw ApiError(detail, expired = r.code == 401 && store.token != null && !request.url.encodedPath.endsWith("/login"))
                }
                obj
            }
        } catch (e: IOException) {
            throw ApiError("Can't reach the server. Check the address and that it is running.")
        }
    }

    private fun authed(path: String) = Request.Builder().url(url(path)).header("Authorization", "Bearer ${store.token}")

    private fun saveLogin(o: JSONObject) {
        val user = o.getJSONObject("user")
        store.token = o.getString("token")
        store.name = user.getString("name")
        store.role = user.getString("role")
    }

    suspend fun login(phone: String, pin: String) {
        val body = JSONObject().put("phone", phone).put("pin", pin).toString().toRequestBody(json)
        saveLogin(call(Request.Builder().url(url("/api/app/login")).post(body).build()))
    }

    suspend fun register(phone: String, pin: String, role: String, name: String, code: String) {
        val o = JSONObject().put("phone", phone).put("pin", pin).put("role", role).put("name", name)
        if (code.isNotBlank()) o.put("setup_code", code.trim())
        saveLogin(call(Request.Builder().url(url("/api/app/register")).post(o.toString().toRequestBody(json)).build()))
    }

    suspend fun chat(text: String): Turn =
        turn(call(authed("/api/app/chat").post(JSONObject().put("text", text).toString().toRequestBody(json)).build()))

    suspend fun voice(file: File): Turn {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("audio", file.name, file.asRequestBody("audio/aac".toMediaType()))
            .build()
        return turn(call(authed("/api/app/voice").post(body).build()))
    }

    suspend fun history(): List<Pair<String, String>> {
        val arr = call(authed("/api/app/history").get().build()).getJSONArray("messages")
        return (0 until arr.length()).map { arr.getJSONObject(it).let { m -> m.getString("role") to m.getString("text") } }
    }

    suspend fun inbox(): List<InboxItem> {
        val arr = call(authed("/api/app/inbox").get().build()).getJSONArray("items")
        return (0 until arr.length()).map {
            val o = arr.getJSONObject(it)
            InboxItem(o.getInt("id"), o.optStringOrNull("text"), o.optStringOrNull("filename"))
        }
    }

    suspend fun logout() {
        runCatching { call(authed("/api/app/logout").post(ByteArray(0).toRequestBody()).build()) }
        store.signOut()
    }

    /** Download a file the server holds for this user into the app's cache, ready to open or share. */
    suspend fun download(id: Int, filename: String, dir: File): File = withContext(Dispatchers.IO) {
        try {
            http.newCall(authed("/api/app/file/$id").get().build()).execute().use { r ->
                if (!r.isSuccessful) throw ApiError("Couldn't download $filename.")
                val safe = filename.replace(Regex("[^A-Za-z0-9._-]"), "_")
                File(dir, safe).also { out -> out.outputStream().use { r.body!!.byteStream().copyTo(it) } }
            }
        } catch (e: IOException) {
            throw ApiError("Can't reach the server.")
        }
    }

    private fun turn(o: JSONObject): Turn {
        fun strings(key: String) = o.optJSONArray(key)?.let { a -> (0 until a.length()).map { a.getString(it) } } ?: emptyList()
        val files = o.optJSONArray("files")?.let { a ->
            (0 until a.length()).map { a.getJSONObject(it).let { f -> FileRef(f.getInt("id"), f.getString("filename")) } }
        } ?: emptyList()
        val shares = o.optJSONArray("shares")?.let { a ->
            (0 until a.length()).map {
                val s = a.getJSONObject(it)
                Share(s.getString("to"), s.optString("text"), if (s.has("file_id")) s.getInt("file_id") else null)
            }
        } ?: emptyList()
        return Turn(o.optStringOrNull("heard"), o.getString("reply"), strings("messages"), files, shares, strings("quick_replies"))
    }
}

private fun JSONObject.optStringOrNull(key: String): String? = if (isNull(key) || !has(key)) null else getString(key)
