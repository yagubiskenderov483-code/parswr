/* (c) Magnus Auvinen. See licence.txt in the root of the distribution for more information. */
/* If you are missing that file, acquire a complete release at teeworlds.com.                */

// This file can be included several times.

#ifndef MACRO_CONFIG_INT
#error "The config macros must be defined"
// This helps IDEs properly syntax highlight the uses of the macro below.
#define MACRO_CONFIG_INT(Name, ScriptName, Def, Min, Max, Save, Desc)
#define MACRO_CONFIG_COL(Name, ScriptName, Def, Save, Desc)
#define MACRO_CONFIG_STR(Name, ScriptName, Len, Def, Save, Desc)
#endif

// client
// NEPTUN_CLIENT_CONFIG_BEGIN
MACRO_CONFIG_INT(ClNeptunEnabled, cl_neptun_enabled, 1, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Enable Neptun Client training features")
MACRO_CONFIG_INT(ClNeptunAutoHook, cl_neptun_auto_hook, 0, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Enable dummy auto-hook training mode")
MACRO_CONFIG_INT(ClNeptunAimTraining, cl_neptun_aim_training, 0, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Enable aim-training HUD/assist mode for dummy training")
MACRO_CONFIG_INT(ClNeptunSpeedProfile, cl_neptun_speed_profile, 100, 25, 300, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Neptun movement profile value (training/config only)")
MACRO_CONFIG_INT(ClNeptunQuick, cl_neptun_quick, 0, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Enable Neptun quick-movement training mode")
MACRO_CONFIG_INT(ClNeptunMoonwalk, cl_neptun_moonwalk, 0, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Enable Neptun moon-walk training mode")
// NEPTUN_CLIENT_CONFIG_END

MACRO_CONFIG_INT(ClPredict, cl_predict, 1, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Predict client movements")
MACRO_CONFIG_INT(ClPredictDummy, cl_predict_dummy, 1, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Predict dummy movements")
MACRO_CONFIG_INT(ClPredictEvents, cl_predict_events, 0, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Predict events (sounds, particles)")
MACRO_CONFIG_INT(ClAntiPingLimit, cl_antiping_limit, 0, 0, 500, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Adds delay to antiping (0 to disable)")
MACRO_CONFIG_INT(ClAntiPingPercent, cl_antiping_percent, 100, 0, 100, CFGFLAG_CLIENT | CFGFLAG_SAVE, "How far ahead antiping predicts, ignored when antiping limit is used")
MACRO_CONFIG_INT(ClAntiPing, cl_antiping, 0, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Enable antiping, i. e. more aggressive prediction.")
MACRO_CONFIG_INT(ClAntiPingPlayers, cl_antiping_players, 1, 0, 3, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Predict other player's movement (only enabled if cl_antiping is set to 1) (1 = always on, 2 = interaction only, 3 = 2 and frozen tees always)")
MACRO_CONFIG_INT(ClAntiPingGrenade, cl_antiping_grenade, 1, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Predict grenades (only enabled if cl_antiping is set to 1)")
MACRO_CONFIG_INT(ClAntiPingWeapons, cl_antiping_weapons, 1, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Predict weapon projectiles (only enabled if cl_antiping is set to 1)")
MACRO_CONFIG_INT(ClAntiPingSmooth, cl_antiping_smooth, 0, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Make the prediction of other player's movement smoother")
MACRO_CONFIG_INT(ClAntiPingGunfire, cl_antiping_gunfire, 1, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Predict gunfire and show predicted weapon physics (with cl_antiping_grenade 1 and cl_antiping_weapons 1)")
MACRO_CONFIG_INT(ClAntiPingPreInput, cl_antiping_preinput, 1, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Predict other players using preinputs for more accurate input prediction")
MACRO_CONFIG_INT(ClPredictionMargin, cl_prediction_margin, 10, 1, 300, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Prediction margin in ms (adds latency, can reduce lag from ping jumps)")
MACRO_CONFIG_INT(ClSubTickAiming, cl_sub_tick_aiming, 0, 0, 1, CFGFLAG_CLIENT | CFGFLAG_SAVE, "Send aiming data at sub-tick accuracy")
