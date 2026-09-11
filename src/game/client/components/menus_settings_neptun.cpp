#include "menus.h"

#include <engine/shared/config.h>
#include <game/client/gameclient.h>
#include <game/client/ui.h>

void CMenus::RenderSettingsNeptun(CUIRect MainView)
{
	CUIRect Button, Left, Right, Label;
	MainView.HSplitTop(30.0f, &Label, &MainView);
	Ui()->DoLabel(&Label, "Neptun Client", 24.0f, TEXTALIGN_ML);
	MainView.HSplitTop(12.0f, nullptr, &MainView);
	MainView.VSplitMid(&Left, &Right, 20.0f);

	Left.HSplitTop(28.0f, &Button, &Left);
	if(DoButton_CheckBox(&g_Config.m_ClNeptunEnabled, "Enable Neptun training", g_Config.m_ClNeptunEnabled, &Button))
		g_Config.m_ClNeptunEnabled ^= 1;
	Left.HSplitTop(28.0f, &Button, &Left);
	if(DoButton_CheckBox(&g_Config.m_ClNeptunAutoHook, "Dummy auto-hook (training)", g_Config.m_ClNeptunAutoHook, &Button))
	{
		g_Config.m_ClNeptunAutoHook ^= 1;
		g_Config.m_ClDummyControl = g_Config.m_ClNeptunAutoHook;
		g_Config.m_ClDummyHook = g_Config.m_ClNeptunAutoHook;
	}
	Left.HSplitTop(28.0f, &Button, &Left);
	if(DoButton_CheckBox(&g_Config.m_ClNeptunAimTraining, "Aim training HUD/assist", g_Config.m_ClNeptunAimTraining, &Button))
		g_Config.m_ClNeptunAimTraining ^= 1;

	Right.HSplitTop(28.0f, &Button, &Right);
	Ui()->DoScrollbarOption(&g_Config.m_ClNeptunSpeedProfile, &g_Config.m_ClNeptunSpeedProfile, &Button, "Speed profile", 25, 300, &CUi::ms_LinearScrollbarScale, CUi::SCROLLBAR_OPTION_NOCLAMPVALUE);
	Right.HSplitTop(28.0f, &Button, &Right);
	if(DoButton_CheckBox(&g_Config.m_ClNeptunQuick, "Quick movement training", g_Config.m_ClNeptunQuick, &Button))
		g_Config.m_ClNeptunQuick ^= 1;
	Right.HSplitTop(28.0f, &Button, &Right);
	if(DoButton_CheckBox(&g_Config.m_ClNeptunMoonwalk, "Moon-walk training", g_Config.m_ClNeptunMoonwalk, &Button))
		g_Config.m_ClNeptunMoonwalk ^= 1;
}
