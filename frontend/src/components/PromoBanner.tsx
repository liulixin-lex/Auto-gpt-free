import { ExternalLink, Radio } from "lucide-react";
import { useI18n } from "@/lib/i18n-context";

const PROMO_URL = "https://gguuai.com";

export default function PromoBanner() {
  const { t } = useI18n();

  return (
    <a
      href={PROMO_URL}
      target="_blank"
      rel="noopener noreferrer"
      className="xy-promo"
      aria-label={`${t("promo.name")} — ${PROMO_URL}`}
    >
      <div className="xy-promo-rail" aria-hidden>
        <Radio className="h-3.5 w-3.5" strokeWidth={2.2} />
      </div>
      <div className="xy-promo-main">
        <div className="xy-promo-meta">
          <span className="xy-k">{t("promo.kicker")}</span>
          <span className="xy-promo-badge">{t("promo.badge")}</span>
        </div>
        <div className="xy-promo-row">
          <div className="min-w-0">
            <h2 className="xy-promo-name">{t("promo.name")}</h2>
            <p className="xy-promo-desc">{t("promo.desc")}</p>
          </div>
          <span className="xy-promo-cta">
            <span className="xy-promo-host">gguuai.com</span>
            <ExternalLink className="h-3.5 w-3.5 shrink-0" strokeWidth={2.2} />
          </span>
        </div>
      </div>
    </a>
  );
}
