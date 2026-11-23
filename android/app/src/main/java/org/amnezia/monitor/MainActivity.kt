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
import androidx.compose.animation.EnterTransition
import androidx.compose.animation.ExitTransition
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.ui.graphics.Color
import androidx.navigation.NavHostController
import androidx.navigation.compose.currentBackStackEntryAsState
import org.amnezia.monitor.data.CountryDetect
import org.amnezia.monitor.data.ServerSummary
import org.amnezia.monitor.ui.AmneziaTheme
import org.amnezia.monitor.ui.LocalAccent
import org.amnezia.monitor.ui.LocalAccent2
import org.amnezia.monitor.ui.LocalAnimations
import org.amnezia.monitor.ui.NeonBackground
import org.amnezia.monitor.ui.systemAnimationsOn
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
    // Оформление: тема открытого сервера (на главной и в настройках — общая «Amnezia»)
    val entry by nav.currentBackStackEntryAsState()
    val theme = when (entry?.destination?.route) {
        "local/{id}" -> vm.themeFor(null, entry?.arguments?.getString("id"))
        "server/{name}" -> vm.themeFor(entry?.arguments?.getString("name"), null)
        else -> CountryDetect.DEFAULT
    }
    val anim = vm.settings.animations && systemAnimationsOn()
    val accent by animateColorAsState(Color(theme.accent), tween(if (anim) 900 else 0), label = "accent")
    val accent2 by animateColorAsState(Color(theme.accent2), tween(if (anim) 900 else 0), label = "accent2")
    val open: (ServerSummary) -> Unit = { server -> nav.openServer(server, replace = false) }
    val switchTo: (ServerSummary) -> Unit = { server -> nav.openServer(server, replace = true) }
    CompositionLocalProvider(LocalAnimations provides anim, LocalAccent provides accent, LocalAccent2 provides accent2) {
    NeonBackground(theme, accent) {
    NavHost(
        navController = nav,
        startDestination = "home",
        enterTransition = { if (anim) fadeIn(tween(450)) else EnterTransition.None },
        exitTransition = { if (anim) fadeOut(tween(450)) else ExitTransition.None },
        popEnterTransition = { if (anim) fadeIn(tween(450)) else EnterTransition.None },
        popExitTransition = { if (anim) fadeOut(tween(450)) else ExitTransition.None },
    ) {
        composable("home") {
            HomeScreen(
                home = vm.home,
                onRefresh = vm::pull,
                onOpen = open,
                onSettings = { nav.navigate("settings") },
                onTrustHostKey = vm::trustHostKey,
                hostKeyMessage = vm.hostKeyMessage,
                onDismissHostKeyMessage = vm::clearHostKeyMessage,
            )
        }
        composable("settings") {
            SettingsScreen(vm = vm, onBack = { nav.popBackStack() })
        }
        composable(
            route = "local/{id}",
            arguments = listOf(navArgument("id") { type = NavType.StringType }),
        ) { entry ->
            val id = entry.arguments?.getString("id").orEmpty()
            DisposableEffect(id) {
                vm.watchLocal(id)
                onDispose { vm.unwatch(localId = id) }
            }
            DetailScreen(
                name = vm.detail.detail?.name ?: "Сервер",
                state = vm.detail,
                onBack = { nav.popBackStack() },
                onRefresh = vm::pull,
                onHours = vm::setHours,
                theme = vm.themeFor(null, id),
                servers = vm.home.servers,
                isCurrent = { it.localId == id },
                onSwitch = switchTo,
            )
        }
        composable(
            route = "server/{name}",
            arguments = listOf(navArgument("name") { type = NavType.StringType }),
        ) { entry ->
            val name = entry.arguments?.getString("name").orEmpty()
            DisposableEffect(name) {
                vm.watchDetail(name)
                onDispose { vm.unwatch(name = name) }
            }
            DetailScreen(
                name = name,
                state = vm.detail,
                onBack = { nav.popBackStack() },
                onRefresh = vm::pull,
                onHours = vm::setHours,
                theme = vm.themeFor(name, null),
                servers = vm.home.servers,
                isCurrent = { it.localId == null && it.name == name },
                onSwitch = switchTo,
            )
        }
    }
    }
    }
}

/** Открыть сервер; replace — переключение с экрана другого сервера (назад вернёт на главную). */
private fun NavHostController.openServer(server: ServerSummary, replace: Boolean) {
    val local = server.localId
    val route = if (local != null) "local/${Uri.encode(local)}" else "server/${Uri.encode(server.name)}"
    navigate(route) {
        if (replace) popUpTo("home")
        launchSingleTop = true
    }
}
