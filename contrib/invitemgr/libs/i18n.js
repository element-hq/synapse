export async function initI18n() {
    const lang = navigator.language.startsWith('ru') ? 'ru' : 'en';
    
    try {
        const response = await fetch(`/libs/lang/${lang}.json`);
        const translations = await response.json();

        window.t = (key) => translations[key] || key;

        document.querySelectorAll('[data-i18n]').forEach(el => {
            const key = el.getAttribute('data-i18n');
            if (translations[key]) {
                if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                    el.placeholder = translations[key];
                } else {
                    el.innerText = translations[key];
                }
            }
        });

        document.dispatchEvent(new Event('i18n:ready'));
    } catch (err) {
        console.error("Failed to load translations:", err);
        window.t = (key) => key; // Fallback
    }
}