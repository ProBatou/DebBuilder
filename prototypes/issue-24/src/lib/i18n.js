import en from '../locales/en.json' with {type: 'json'};
import fr from '../locales/fr.json' with {type: 'json'};
import de from '../locales/de.json' with {type: 'json'};
import es from '../locales/es.json' with {type: 'json'};

export const catalogs = {en, fr, de, es};
export const localeNames = {en: 'English', fr: 'Français', de: 'Deutsch', es: 'Español'};
export const localeTags = {en: 'en-US', fr: 'fr-FR', de: 'de-DE', es: 'es-ES'};
export function translate(locale, key, values = {}) {
  const message = catalogs[locale]?.[key] ?? en[key] ?? key;
  return message.replace(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g, (_, name) =>
    Object.hasOwn(values, name) ? String(values[name]) : `{${name}}`);
}
export function formatCount(locale, count) {
  return new Intl.NumberFormat(localeTags[locale] || localeTags.en).format(count);
}
export function formatWhen(locale, iso) {
  return new Intl.DateTimeFormat(localeTags[locale] || localeTags.en, {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(iso));
}
export function countMessage(locale, singularKey, pluralKey, count) {
  const plural = new Intl.PluralRules(localeTags[locale] || localeTags.en).select(count) === 'one';
  return translate(locale, plural ? singularKey : pluralKey, {count: formatCount(locale,count)});
}
