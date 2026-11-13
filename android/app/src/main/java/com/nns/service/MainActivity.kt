package com.nns.service

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

private const val DEFAULT_API = "http://10.0.2.2:8080"
private val http = OkHttpClient()

data class Master(
    val id: Int,
    val name: String,
    val tgId: Long,
    val role: String,
    val active: Boolean
)

data class Client(
    val id: Int,
    val username: String,
    val tgId: Long,
    val master: String
)

data class Car(
    val id: Int,
    val brand: String,
    val model: String,
    val year: Int,
    val vin: String,
    val mileage: Int,
    val engine: String,
    val gearbox: String,
    val plate: String
)

data class ServiceRow(
    val id: Int,
    val date: String,
    val mileage: Int,
    val work: String,
    val description: String,
    val price: Double
)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { NNSApp() }
    }
}

@Composable
fun NNSApp() {
    val context = LocalContext.current
    var tg by remember { mutableStateOf("") }
    var serverUrl by remember {
        mutableStateOf(
            context.getSharedPreferences("nns", android.content.Context.MODE_PRIVATE)
                .getString("server_url", DEFAULT_API) ?: DEFAULT_API
        )
    }
    var token by remember { mutableStateOf<String?>(null) }
    var master by remember { mutableStateOf<Master?>(null) }
    var screen by remember { mutableStateOf("clients") }
    var clients by remember { mutableStateOf(emptyList<Client>()) }
    var selected by remember { mutableStateOf<Client?>(null) }
    var masters by remember { mutableStateOf(emptyList<Master>()) }
    var error by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var clientCars by remember { mutableStateOf(emptyList<Car>()) }
    var selectedCar by remember { mutableStateOf<Car?>(null) }
    var services by remember { mutableStateOf(emptyList<ServiceRow>()) }
    val scope = rememberCoroutineScope()

    fun api(path: String, method: String = "GET", body: String? = null): String {
        val requestBody = body?.toRequestBody("application/json".toMediaType())
        val builder = Request.Builder()
            .url(serverUrl.trim().trimEnd('/') + path)
            .header("x-token", token ?: "")

        when (method) {
            "POST" -> builder.post(requestBody!!)
            "PATCH" -> builder.patch(requestBody!!)
        }

        val response = http.newCall(builder.build()).execute()
        val text = response.body?.string() ?: "{}"
        if (!response.isSuccessful) {
            throw Exception(JSONObject(text).optString("detail", "HTTP ${response.code}"))
        }
        return text
    }

    fun refreshClients() {
        scope.launch {
            busy = true
            try {
                val json = JSONArray(withContext(Dispatchers.IO) { api("/clients") })
                clients = buildList {
                    for (i in 0 until json.length()) {
                        val item = json.getJSONObject(i)
                        add(
                            Client(
                                id = item.getInt("id"),
                                username = item.optString("username"),
                                tgId = item.optLong("tg_id"),
                                master = item.optString("master_name")
                            )
                        )
                    }
                }
                error = ""
            } catch (e: Exception) {
                error = e.message ?: "Ошибка"
            }
            busy = false
        }
    }

    MaterialTheme {
        Surface(modifier = Modifier.fillMaxSize()) {
            when {
                token == null -> {
                    LoginScreen(
                        tg = tg,
                        onTg = { tg = it },
                        serverUrl = serverUrl,
                        onServerUrl = { serverUrl = it },
                        error = error,
                        onLogin = {
                            scope.launch {
                                busy = true
                                try {
                                    val normalizedUrl = serverUrl.trim().trimEnd('/')
                                    require(normalizedUrl.startsWith("http://") || normalizedUrl.startsWith("https://")) { "Адрес сервера должен начинаться с http:// или https://" }
                                    context.getSharedPreferences("nns", android.content.Context.MODE_PRIVATE).edit().putString("server_url", normalizedUrl).apply()
                                    val result = withContext(Dispatchers.IO) { apiLogin(normalizedUrl, tg) }
                                    token = result.first
                                    master = result.second
                                    refreshClients()
                                } catch (e: Exception) {
                                    error = e.message ?: "Ошибка"
                                }
                                busy = false
                            }
                        }
                    )
                }

                screen == "clients" -> {
                    ClientsScreen(
                        m = master!!,
                        cs = clients,
                        busy = busy,
                        error = error,
                        reload = { refreshClients() },
                        open = { client ->
                            selected = client
                            selectedCar = null
                            services = emptyList()
                            screen = "client"
                        },
                        masters = { screen = "masters" }
                    )
                }

                screen == "masters" -> {
                    MastersScreen(
                        m = master!!,
                        ms = masters,
                        error = error,
                        reload = {
                            scope.launch {
                                try {
                                    val json = JSONArray(
                                        withContext(Dispatchers.IO) { api("/masters") }
                                    )
                                    masters = buildList {
                                        for (i in 0 until json.length()) {
                                            val item = json.getJSONObject(i)
                                            add(
                                                Master(
                                                    id = item.getInt("id"),
                                                    name = item.optString("name"),
                                                    tgId = item.optLong("tg_id"),
                                                    role = item.optString("role"),
                                                    active = item.optInt("active") == 1
                                                )
                                            )
                                        }
                                    }
                                    error = ""
                                } catch (e: Exception) {
                                    error = e.message ?: "Ошибка"
                                }
                            }
                        },
                        back = { screen = "clients" },
                        onAdd = { }
                    )
                }

                else -> {
                    val client = selected
                    if (client != null) {
                        ClientScreen(
                            c = client,
                            m = master!!,
                            cars = clientCars,
                            selected = selectedCar,
                            services = services,
                            error = error,
                            load = {
                                scope.launch {
                                    try {
                                        val json = JSONObject(
                                            withContext(Dispatchers.IO) {
                                                api("/clients/${client.id}")
                                            }
                                        )
                                        val carsJson = json.optJSONArray("cars") ?: JSONArray()
                                        clientCars = buildList {
                                            for (i in 0 until carsJson.length()) {
                                                val item = carsJson.getJSONObject(i)
                                                add(
                                                    Car(
                                                        id = item.getInt("id"),
                                                        brand = item.optString("brand"),
                                                        model = item.optString("model"),
                                                        year = item.optInt("year"),
                                                        vin = item.optString("vin"),
                                                        mileage = item.optInt("mileage"),
                                                        engine = item.optString("engine"),
                                                        gearbox = item.optString("gearbox"),
                                                        plate = item.optString("registration_number")
                                                    )
                                                )
                                            }
                                        }
                                        error = ""
                                    } catch (e: Exception) {
                                        error = e.message ?: "Ошибка"
                                    }
                                }
                            },
                            select = { car ->
                                selectedCar = car
                                scope.launch {
                                    try {
                                        val json = JSONObject(
                                            withContext(Dispatchers.IO) {
                                                api("/cars/${car.id}")
                                            }
                                        )
                                        val serviceJson =
                                            json.optJSONArray("services") ?: JSONArray()
                                        services = buildList {
                                            for (i in 0 until serviceJson.length()) {
                                                val item = serviceJson.getJSONObject(i)
                                                add(
                                                    ServiceRow(
                                                        id = item.getInt("id"),
                                                        date = item.optString("service_date"),
                                                        mileage = item.optInt("mileage"),
                                                        work = item.optString("work_name"),
                                                        description = item.optString("description"),
                                                        price = item.optDouble("price")
                                                    )
                                                )
                                            }
                                        }
                                        error = ""
                                    } catch (e: Exception) {
                                        error = e.message ?: "Ошибка"
                                    }
                                }
                            },
                            back = {
                                selected = null
                                selectedCar = null
                                services = emptyList()
                                screen = "clients"
                            }
                        )
                    } else {
                        screen = "clients"
                    }
                }
            }
        }
    }
}

fun apiLogin(baseUrl: String, tg: String): Pair<String, Master> {
    val request = Request.Builder()
        .url(baseUrl.trim().trimEnd('/') + "/auth")
        .post(
            JSONObject()
                .put("tg_id", tg.toLong())
                .toString()
                .toRequestBody("application/json".toMediaType())
        )
        .build()

    val response = http.newCall(request).execute()
    val text = response.body!!.string()

    if (!response.isSuccessful) {
        throw Exception(JSONObject(text).optString("detail", "Ошибка входа"))
    }

    val json = JSONObject(text)
    val item = json.getJSONObject("master")
    val master = Master(
        id = item.getInt("id"),
        name = item.getString("name"),
        tgId = item.getLong("tg_id"),
        role = item.getString("role"),
        active = true
    )

    return json.getString("token") to master
}

@Composable
fun LoginScreen(
    tg: String,
    onTg: (String) -> Unit,
    serverUrl: String,
    onServerUrl: (String) -> Unit,
    error: String,
    onLogin: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center
    ) {
        Text("NNS Service", style = MaterialTheme.typography.headlineLarge)
        Text("Мобильное приложение автосервиса")
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(
            value = serverUrl,
            onValueChange = onServerUrl,
            label = { Text("Адрес сервера API") },
            placeholder = { Text("http://192.168.1.100:8080") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = tg,
            onValueChange = onTg,
            label = { Text("Telegram ID мастера") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(Modifier.height(12.dp))
        Button(onClick = onLogin, modifier = Modifier.fillMaxWidth()) {
            Text("Войти")
        }
        if (error.isNotEmpty()) {
            Spacer(Modifier.height(10.dp))
            Text(error, color = MaterialTheme.colorScheme.error)
        }
    }
}

@Composable
fun ClientsScreen(
    m: Master,
    cs: List<Client>,
    busy: Boolean,
    error: String,
    reload: () -> Unit,
    open: (Client) -> Unit,
    masters: () -> Unit
) {
    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Column {
                Text("NNS Service", style = MaterialTheme.typography.headlineSmall)
                Text("${m.name} · ${if (m.role == "admin") "администратор" else "мастер"}")
            }
            Row {
                if (m.role == "admin") {
                    TextButton(onClick = masters) { Text("Мастера") }
                }
                TextButton(onClick = reload) { Text("Обновить") }
            }
        }

        if (error.isNotEmpty()) {
            Text(error, color = MaterialTheme.colorScheme.error)
        }

        Spacer(Modifier.height(8.dp))

        if (busy) {
            LinearProgressIndicator(Modifier.fillMaxWidth())
        }

        LazyColumn {
            items(cs) { c ->
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp),
                    onClick = { open(c) }
                ) {
                    Column(Modifier.padding(14.dp)) {
                        Text(
                            "#${c.id} ${if (c.username.isNotBlank()) "@${c.username}" else ""}",
                            style = MaterialTheme.typography.titleMedium
                        )
                        Text("Telegram: ${c.tgId}")
                        if (m.role == "admin") {
                            Text("Мастер: ${c.master}")
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun ClientScreen(
    c: Client,
    m: Master,
    cars: List<Car>,
    selected: Car?,
    services: List<ServiceRow>,
    error: String,
    load: () -> Unit,
    select: (Car) -> Unit,
    back: () -> Unit
) {
    LaunchedEffect(c.id) { load() }

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        TextButton(onClick = back) { Text("← Клиенты") }
        Text("Клиент #${c.id}", style = MaterialTheme.typography.headlineSmall)
        Text(if (c.username.isNotBlank()) "@${c.username}" else "Telegram ${c.tgId}")

        if (m.role == "admin") {
            Text("Мастер: ${c.master}")
        }

        Spacer(Modifier.height(12.dp))
        Text("Автомобили", style = MaterialTheme.typography.titleLarge)

        LazyColumn {
            items(cars) { car ->
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp),
                    onClick = { select(car) }
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text(
                            "${car.brand} ${car.model}",
                            style = MaterialTheme.typography.titleMedium
                        )
                        Text("${car.year} · ${car.engine} · ${car.gearbox}")
                        Text(
                            "Пробег: ${car.mileage} км" +
                                if (car.plate.isNotBlank()) " · ${car.plate}" else ""
                        )
                    }
                }
            }
        }

        selected?.let { car ->
            Spacer(Modifier.height(10.dp))
            Text(
                "Сервис: ${car.brand} ${car.model}",
                style = MaterialTheme.typography.titleLarge
            )

            if (services.isEmpty()) {
                Text("История пока пустая")
            } else {
                services.forEach { service ->
                    Card(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(vertical = 3.dp)
                    ) {
                        Column(Modifier.padding(10.dp)) {
                            Text("${service.date} · ${service.mileage} км")
                            Text(service.work)
                            if (service.description.isNotBlank()) {
                                Text(service.description)
                            }
                            if (service.price != 0.0) {
                                Text("${service.price} €")
                            }
                        }
                    }
                }
            }
        }

        if (error.isNotEmpty()) {
            Text(error, color = MaterialTheme.colorScheme.error)
        }
    }
}

@Composable
fun MastersScreen(
    m: Master,
    ms: List<Master>,
    error: String,
    reload: () -> Unit,
    back: () -> Unit,
    onAdd: () -> Unit
) {
    LaunchedEffect(Unit) { reload() }

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        TextButton(onClick = back) { Text("← Клиенты") }

        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text("Мастера", style = MaterialTheme.typography.headlineSmall)
            if (m.role == "admin") {
                TextButton(onClick = onAdd) { Text("+ Добавить") }
            }
        }

        if (error.isNotEmpty()) {
            Text(error, color = MaterialTheme.colorScheme.error)
        }

        LazyColumn {
            items(ms) { x ->
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp)
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text(x.name, style = MaterialTheme.typography.titleMedium)
                        Text("Telegram: ${x.tgId}")
                        Text(
                            if (x.role == "admin") {
                                "Администратор"
                            } else if (x.active) {
                                "Активен"
                            } else {
                                "Отключён"
                            }
                        )
                    }
                }
            }
        }
    }
}
