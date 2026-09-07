package com.insideside.vinylplayer

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.*
import android.widget.ProgressBar
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var serverStarted = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webview)

        // Initialize Chaquopy Python
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        val py = Python.getInstance()
        val os = py.getModule("os")

        // Set Android paths for Python
        os.callAttr("putenv", "VINYL_HOME", filesDir.absolutePath)
        val musicDir = getExternalFilesDir("VinylMusic")
        musicDir?.mkdirs()
        os.callAttr("putenv", "VINYL_MUSIC_DIR", musicDir?.absolutePath ?: filesDir.absolutePath)

        // Start Python server in background
        Thread {
            try {
                val launcher = py.getModule("android_launcher")
                val port = launcher.callAttr("start_server").toInt()
                serverStarted = true

                // Wait for server to be ready
                Thread.sleep(800)

                runOnUiThread { setupWebView(port) }
            } catch (e: Exception) {
                e.printStackTrace()
                runOnUiThread {
                    findViewById<ProgressBar>(R.id.loading).visibility = View.GONE
                    webView.loadData(
                        "<html><body style='background:#111;color:#e94560;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>" +
                        "<div style='text-align:center'><h2>Ошибка запуска</h2><p style='color:#888'>${e.message}</p></div></body></html>",
                        "text/html", "utf-8"
                    )
                }
            }
        }.start()

        // Start foreground service for background playback
        val serviceIntent = Intent(this, PlayerService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView(port: Int) {
        findViewById<ProgressBar>(R.id.loading).visibility = View.GONE

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = true
            databaseEnabled = true
            cacheMode = WebSettings.LOAD_DEFAULT
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            userAgentString = userAgentString + " VinylPlayerAndroid"
        }

        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView?, request: WebResourceRequest?, error: WebResourceError?
            ) {
                // Retry on connection error (server might not be ready)
                if (request?.isForMainFrame == true) {
                    view?.postDelayed({ view.reload() }, 1000)
                }
            }
        }

        webView.webChromeClient = WebChromeClient()
        webView.loadUrl("http://127.0.0.1:$port")
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            moveTaskToBack(true)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        stopService(Intent(this, PlayerService::class.java))
    }
}
