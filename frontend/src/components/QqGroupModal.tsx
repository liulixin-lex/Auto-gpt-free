import { useEffect, useId } from "react";
import { X } from "lucide-react";
import { useI18n } from "@/lib/i18n-context";

const QR_SRC = "/assets/qq-group.png";

type Props = {
  open: boolean;
  onClose: () => void;
};

export default function QqGroupModal({ open, onClose }: Props) {
  const { t } = useI18n();
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="dialog-backdrop xy-qq-backdrop"
      role="presentation"
      onClick={onClose}
    >
      <div
        className="dialog-panel xy-qq-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="xy-qq-head">
          <div>
            <div className="xy-k">QQ · GROUP</div>
            <h2 id={titleId} className="xy-qq-title">
              {t("qqModal.title")}
            </h2>
          </div>
          <button
            type="button"
            className="xy-icon-btn"
            onClick={onClose}
            aria-label={t("common.close")}
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="xy-qq-body">
          <div className="xy-qq-frame">
            <img
              src={QR_SRC}
              alt={t("qqModal.qrAlt")}
              className="xy-qq-img"
              decoding="async"
            />
          </div>
          <p id={descId} className="xy-qq-caption">
            {t("qqModal.caption")}
          </p>
        </div>

        <footer className="xy-qq-foot">
          <button type="button" className="xy-gate-submit xy-qq-ok" onClick={onClose}>
            {t("qqModal.ack")}
          </button>
        </footer>
      </div>
    </div>
  );
}
