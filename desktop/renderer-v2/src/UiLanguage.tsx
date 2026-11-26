// 管理界面语言及翻译上下文，同步桌面菜单使用的语言。
// 主要引用方包括 `App.tsx` 和使用产品文案的界面组件。
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { translator } from "./uiMessages";

export type Locale = "zh-CN" | "en";
export type LanguageChoice = "system" | Locale;
export type LanguageSetting = { preference: LanguageChoice; locale: Locale };
const translations = { en: translator("en"), "zh-CN": translator("zh-CN") };
type LanguageContext = LanguageSetting & {
  t: ReturnType<typeof translator>;
  choose: (choice: LanguageChoice) => Promise<void>;
  saving: boolean;
  error: boolean;
};
const Context = createContext<LanguageContext | null>(null);

export function UiLanguageProvider({ children }: { children: ReactNode }) {
  const [setting, setSetting] = useState<LanguageSetting>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);
  const revision = useRef(0);
  const pending = useRef(false);
  useEffect(() => {
    let active = true;
    async function refresh() {
      const request = revision.current;
      if (pending.current) return;
      try {
        const saved = await window.workbench.displayLanguage();
        if (active && request === revision.current) { setSetting(saved); setError(false); }
      } catch {
        if (active && request === revision.current) {
          const first = navigator.languages.find(tag => /^(?:zh|en)(?:-|$)/i.test(tag));
          setSetting(old => old ?? { preference: "system", locale: /^zh/i.test(first ?? "") ? "zh-CN" : "en" });
          setError(true);
        }
      }
    }
    void refresh();
    window.addEventListener("languagechange", refresh);
    return () => { active = false; window.removeEventListener("languagechange", refresh); };
  }, []);
  const choose = useCallback(async (choice: LanguageChoice) => {
    if (pending.current) return;
    pending.current = true;
    ++revision.current;
    setSaving(true);
    try { setSetting(await window.workbench.displayLanguage(choice)); setError(false); }
    catch { setError(true); }
    finally { pending.current = false; setSaving(false); }
  }, []);
  useLayoutEffect(() => { if (setting) document.documentElement.lang = setting.locale; }, [setting?.locale]);
  if (!setting) return null;
  return <Context.Provider value={{ ...setting, t: translations[setting.locale], choose, saving, error }}>{children}</Context.Provider>;
}

export function useUiLanguage() {
  const language = useContext(Context);
  if (!language) throw Error("Missing display language provider");
  return language;
}
