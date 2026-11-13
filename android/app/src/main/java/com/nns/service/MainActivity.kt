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
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

private const val API = "http://10.0.2.2:8080" // Android Emulator. For a phone replace with http://SERVER_IP:8080 or HTTPS.
private val http = OkHttpClient()

data class Master(val id:Int,val name:String,val tgId:Long,val role:String,val active:Boolean)
data class Client(val id:Int,val username:String,val tgId:Long,val master:String)
data class Car(val id:Int,val brand:String,val model:String,val year:Int,val vin:String,val mileage:Int,val engine:String,val gearbox:String,val plate:String)
data class ServiceRow(val id:Int,val date:String,val mileage:Int,val work:String,val description:String,val price:Double)

class MainActivity: ComponentActivity(){ override fun onCreate(b:Bundle?){super.onCreate(b);setContent{NNSApp()}} }

@Composable fun NNSApp(){
    var tg by remember{mutableStateOf("")}; var token by remember{mutableStateOf<String?>(null)}; var master by remember{mutableStateOf<Master?>(null)}
    var screen by remember{mutableStateOf("clients")}; var clients by remember{mutableStateOf(emptyList<Client>())}; var selected by remember{mutableStateOf<Client?>(null)}
    var masters by remember{mutableStateOf(emptyList<Master>())}; var error by remember{mutableStateOf("")}; var busy by remember{mutableStateOf(false)}
    var clientCars by remember{mutableStateOf(emptyList<Car>())}; var selectedCar by remember{mutableStateOf<Car?>(null)}; var services by remember{mutableStateOf(emptyList<ServiceRow>())}
    val scope=rememberCoroutineScope()
    fun api(path:String,method:String="GET",body:String?=null):String{ val b=body?.toRequestBody("application/json".toMediaType()); val rb=Request.Builder().url(API+path).header("x-token",token?:""); if(method=="POST") rb.post(b!!); else if(method=="PATCH") rb.patch(b!!); val r=http.newCall(rb.build()).execute(); val text=r.body?.string()?:("{}"); if(!r.isSuccessful) throw Exception(JSONObject(text).optString("detail","HTTP ${r.code}")); return text }
    fun refreshClients(){scope.launch{busy=true;try{val a=JSONArray(withContext(Dispatchers.IO){api("/clients")});clients=buildList{for(i in 0 until a.length()){val j=a.getJSONObject(i);add(Client(j.getInt("id"),j.optString("username"),j.optLong("tg_id"),j.optString("master_name")))}};error=""}catch(e:Exception){error=e.message?:"РћС€РёР±РєР°"};busy=false}}
    MaterialTheme { Surface(Modifier.fillMaxSize()) {
        if(token==null) LoginScreen(tg,{tg=it},error){scope.launch{busy=true;try{val t=withContext(Dispatchers.IO){apiLogin(tg)};token=t.first;master=t.second;refreshClients()}catch(e:Exception){error=e.message?:"РћС€РёР±РєР°"};busy=false}}
        else if(screen=="clients") ClientsScreen(master!!,clients,busy,error,{refreshClients()},{c->selected=c;screen="client"},{screen="masters"})
        else if(screen=="masters") MastersScreen(master!!,masters,error,{scope.launch{try{val a=JSONArray(withContext(Dispatchers.IO){api("/masters")});masters=buildList{for(i in 0 until a.length()){val j=a.getJSONObject(i);add(Master(j.getInt("id"),j.optString("name"),j.optLong("tg_id"),j.optString("role"),j.optInt("active")==1))}}}catch(e:Exception){error=e.message?:"РћС€РёР±РєР°"}}},{screen="clients"},{})
        else ClientScreen(selected!!,master!!,clientCars,selectedCar,services,error,{scope.launch{try{val j=JSONObject(withContext(Dispatchers.IO){api("/clients/${selected!!.id}")});clientCars=buildList{val a=j.getJSONArray("cars");for(i in 0 until a.length()){val x=a.getJSONObject(i);add(Car(x.getInt("id"),x.optString("brand"),x.optString("model"),x.optInt("year"),x.optString("vin"),x.optInt("mileage"),x.optString("engine"),x.optString("gearbox"),x.optString("registration_number")))}}}catch(e:Exception){error=e.message?:"РћС€РёР±РєР°"}}},{c->selectedCar=c;scope.launch{try{val j=JSONObject(withContext(Dispatchers.IO){api("/cars/${c.id}")});services=buildList{val a=j.optJSONArray("services")?:JSONArray();for(i in 0 until a.length()){val x=a.getJSONObject(i);add(ServiceRow(x.getInt("id"),x.optString("service_date"),x.optInt("mileage"),x.optString("work_name"),x.optString("description"),x.optDouble("price")))}}}catch(e:Exception){error=e.message?:"РћС€РёР±РєР°"}}},{screen="clients"})
    }}
}

fun apiLogin(tg:String):Pair<String,Master>{ val r=http.newCall(Request.Builder().url(API+"/auth").post(JSONObject().put("tg_id",tg.toLong()).toString().toRequestBody("application/json".toMediaType())).build()).execute(); val s=r.body!!.string(); if(!r.isSuccessful)throw Exception(JSONObject(s).optString("detail","РћС€РёР±РєР° РІС…РѕРґР°")); val j=JSONObject(s); val m=j.getJSONObject("master"); return j.getString("token") to Master(m.getInt("id"),m.getString("name"),m.getLong("tg_id"),m.getString("role"),true) }

@Composable fun LoginScreen(tg:String,onTg:(String)->Unit,error:String,onLogin:()->Unit){Column(Modifier.fillMaxSize().padding(24.dp),verticalArrangement=Arrangement.Center){Text("NNS Service",style=MaterialTheme.typography.headlineLarge);Text("РњРѕР±РёР»СЊРЅРѕРµ РїСЂРёР»РѕР¶РµРЅРёРµ Р°РІС‚РѕСЃРµСЂРІРёСЃР°",style=MaterialTheme.typography.bodyLarge);Spacer(Modifier.height(24.dp));OutlinedTextField(tg,onTg,label={Text("Telegram ID РјР°СЃС‚РµСЂР°")},singleLine=true,modifier=Modifier.fillMaxWidth());Spacer(Modifier.height(12.dp));Button(onClick=onLogin,modifier=Modifier.fillMaxWidth()){Text("Р’РѕР№С‚Рё")};if(error.isNotEmpty()){Spacer(Modifier.height(10.dp));Text(error,color=MaterialTheme.colorScheme.error)}}}

@Composable fun ClientsScreen(m:Master,cs:List<Client>,busy:Boolean,error:String,reload:()->Unit,open:(Client)->Unit,masters:()->Unit){Column(Modifier.fillMaxSize().padding(16.dp)){Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.SpaceBetween){Column{Text("NNS Service",style=MaterialTheme.typography.headlineSmall);Text("${m.name} В· ${if(m.role=="admin")"Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ" else "РјР°СЃС‚РµСЂ"}")};Row{if(m.role=="admin")TextButton(onClick=masters){Text("РњР°СЃС‚РµСЂР°")};TextButton(onClick=reload){Text("РћР±РЅРѕРІРёС‚СЊ")}}};if(error.isNotEmpty())Text(error,color=MaterialTheme.colorScheme.error);Spacer(Modifier.height(8.dp));if(busy)LinearProgressIndicator(Modifier.fillMaxWidth());LazyColumn{items(cs){c->Card(onClick={open(c)},modifier=Modifier.fillMaxWidth().padding(vertical=4.dp)){Column(Modifier.padding(14.dp)){Text("#${c.id} ${if(c.username.isNotBlank())"@${c.username}" else ""}",style=MaterialTheme.typography.titleMedium);Text("Telegram: ${c.tgId}");if(m.role=="admin")Text("РњР°СЃС‚РµСЂ: ${c.master}")}}}}}}

@Composable fun ClientScreen(c:Client,m:Master,cars:List<Car>,selected:Car?,services:List<ServiceRow>,error:String,load:()->Unit,select:(Car)->Unit,back:()->Unit){LaunchedEffect(c.id){load()};Column(Modifier.fillMaxSize().padding(16.dp)){TextButton(onClick=back){Text("в†ђ РљР»РёРµРЅС‚С‹")};Text("РљР»РёРµРЅС‚ #${c.id}",style=MaterialTheme.typography.headlineSmall);Text(if(c.username.isNotBlank())"@${c.username}" else "Telegram ${c.tgId}");if(m.role=="admin")Text("РњР°СЃС‚РµСЂ: ${c.master}");Spacer(Modifier.height(12.dp));Text("РђРІС‚РѕРјРѕР±РёР»Рё",style=MaterialTheme.typography.titleLarge);LazyColumn{items(cars){car->Card(onClick={select(car)},modifier=Modifier.fillMaxWidth().padding(vertical=4.dp)){Column(Modifier.padding(12.dp)){Text("${car.brand} ${car.model}",style=MaterialTheme.typography.titleMedium);Text("${car.year} В· ${car.engine} В· ${car.gearbox}");Text("РџСЂРѕР±РµРі: ${car.mileage} РєРј${if(car.plate.isNotBlank())" В· ${car.plate}" else "")}}}}};selected?.let{car->Spacer(Modifier.height(10.dp));Text("РЎРµСЂРІРёСЃ: ${car.brand} ${car.model}",style=MaterialTheme.typography.titleLarge);if(services.isEmpty())Text("РСЃС‚РѕСЂРёСЏ РїРѕРєР° РїСѓСЃС‚Р°СЏ") else services.forEach{s->Card(Modifier.fillMaxWidth().padding(vertical=3.dp)){Column(Modifier.padding(10.dp)){Text("${s.date} В· ${s.mileage} РєРј");Text(s.work);if(s.description.isNotBlank())Text(s.description);if(s.price!=0.0)Text("${s.price} в‚¬")}}}};if(error.isNotEmpty())Text(error,color=MaterialTheme.colorScheme.error)}}

@Composable fun MastersScreen(m:Master,ms:List<Master>,error:String,reload:()->Unit,back:()->Unit,onAdd:()->Unit){LaunchedEffect(Unit){reload()};Column(Modifier.fillMaxSize().padding(16.dp)){TextButton(onClick=back){Text("в†ђ РљР»РёРµРЅС‚С‹")};Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.SpaceBetween){Text("РњР°СЃС‚РµСЂР°",style=MaterialTheme.typography.headlineSmall);TextButton(onClick=onAdd){Text("+ Р”РѕР±Р°РІРёС‚СЊ")}};if(error.isNotEmpty())Text(error,color=MaterialTheme.colorScheme.error);LazyColumn{items(ms){x->Card(Modifier.fillMaxWidth().padding(vertical=4.dp)){Column(Modifier.padding(12.dp)){Text(x.name,style=MaterialTheme.typography.titleMedium);Text("Telegram: ${x.tgId}");Text(if(x.role=="admin")"РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ" else if(x.active)"РђРєС‚РёРІРµРЅ" else "РћС‚РєР»СЋС‡С‘РЅ")}}}}}}
