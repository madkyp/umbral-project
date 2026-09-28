-- Umbral: fragmento sugerido para ~/.config/hypr/hyprland.lua
-- (revísalo antes de pegarlo; Umbral no toca tu configuración)

-- La propia app: ventana flotante centrada, tamaño cómodo
hl.window_rule({
    name = "umbral",
    match = { class = "^dev\\.madky\\.Umbral$" },
    float = true,
    center = true,
    size = "1100 720",
})

-- Battle.net (XWayland): flotante y sin desenfoque (su interfaz CEF no lo necesita)
hl.window_rule({
    name = "umbral-battlenet",
    match = { title = "(^|.* )Battle\\.net$" },
    float = true,
    center = true,
    no_blur = true,
})

-- World of Warcraft: opaco, sin suspensión y marcado como juego
hl.window_rule({
    name = "umbral-wow",
    match = { title = "^World of Warcraft$" },
    opaque = true,
    no_blur = true,
    idle_inhibit = "fullscreen",
    content = "game",
})

-- Atajo: SUPER + G abre Umbral
hl.bind("SUPER + G", hl.dsp.exec_cmd("umbral"), {
    description = "[Utilities] Umbral (Battle.net)",
})

-- NVIDIA: HyDE ya exporta LIBVA_DRIVER_NAME=nvidia, __GLX_VENDOR_LIBRARY_NAME=nvidia
-- y NVD_BACKEND=direct. No hace falta repetirlas. Si NO usas HyDE, añade:
-- hl.env("LIBVA_DRIVER_NAME", "nvidia")
-- hl.env("__GLX_VENDOR_LIBRARY_NAME", "nvidia")

