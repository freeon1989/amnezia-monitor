package org.amnezia.monitor

import android.app.Application
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.ui.platform.LocalContext
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import org.amnezia.monitor.ui.AmneziaTheme
import org.amnezia.monitor.ui.DetailScreen
import org.amnezia.monitor.ui.HomeScreen
import org.amnezia.monitor.ui.SettingsScreen

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            val app = LocalContext.current.applicationContext as Application
            val vm: MonitorViewModel = viewModel(factory = MonitorViewModel.factory(app))
            AmneziaTheme {
                MonitorApp(vm)
            }
        }
    }
}

@Composable
private fun MonitorApp(vm: MonitorViewModel) {
    val nav = rememberNavController()
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val obs = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_START -> vm.setForeground(true)
                Lifecycle.Event.ON_STOP -> vm.setForeground(false)
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(obs)
        if (lifecycleOwner.lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) {
            vm.setForeground(true)
        }
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(obs)
            vm.setForeground(false)
        }
    }
    NavHost(navController = nav, startDestination = "home") {
        composable("home") {
            HomeScreen(
                home = vm.home,
                onRefresh = vm::pull,
                onOpen = { name -> nav.navigate("server/${Uri.encode(name)}") },
                onSettings = { nav.navigate("settings") },
            )
        }
        composable("settings") {
            SettingsScreen(vm = vm, onBack = { nav.popBackStack() })
        }
        composable(
            route = "server/{name}",
            arguments = listOf(navArgument("name") { type = NavType.StringType }),
        ) { entry ->
            val name = entry.arguments?.getString("name").orEmpty()
            DisposableEffect(name) {
                vm.watchDetail(name)
                onDispose { vm.watchDetail(null) }
            }
            DetailScreen(
                name = name,
                state = vm.detail,
                onBack = { nav.popBackStack() },
                onRefresh = vm::pull,
                onHours = vm::setHours,
            )
        }
    }
}
