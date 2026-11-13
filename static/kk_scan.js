(() => {
  const imageInput = document.getElementById("kk-image");
  const preview = document.getElementById("kk-preview");
  const scanButton = document.getElementById("scan-kk");
  const scanStatus = document.getElementById("scan-status");
  const ocrText = document.getElementById("ocr-text");
  const kkNumber = document.getElementById("kk-number");
  const membersContainer = document.getElementById("kk-members");
  const saveButton = document.getElementById("save-kk");
  const addMemberButton = document.getElementById("add-kk-member");
  if (!imageInput || !preview || !scanButton || !membersContainer) return;

  let selectedImage = null;
  let previewUrl = null;

  function addMember(nationalId = "") {
    if (membersContainer.querySelectorAll(".scan-member").length >= 20) {
      scanStatus.textContent = "Maksimal 20 anggota keluarga per sekali impor.";
      return;
    }

    membersContainer.querySelector(".scan-empty")?.remove();
    const row = document.createElement("div");
    row.className = "scan-member";

    const nameLabel = document.createElement("label");
    nameLabel.textContent = "Nama lengkap";
    const nameInput = document.createElement("input");
    nameInput.name = "resident_name";
    nameInput.maxLength = 100;
    nameInput.autocomplete = "off";
    nameInput.required = true;
    nameInput.placeholder = "Periksa dan isi sesuai KK";
    nameLabel.append(nameInput);

    const idLabel = document.createElement("label");
    idLabel.textContent = "NIK";
    const idInput = document.createElement("input");
    idInput.name = "national_id";
    idInput.inputMode = "numeric";
    idInput.maxLength = 16;
    idInput.pattern = "[0-9]{16}";
    idInput.required = true;
    idInput.value = nationalId;
    idInput.placeholder = "16 angka";
    idLabel.append(idInput);

    const removeButton = document.createElement("button");
    removeButton.className = "delete-button";
    removeButton.type = "button";
    removeButton.textContent = "×";
    removeButton.setAttribute("aria-label", "Hapus anggota dari impor");
    removeButton.addEventListener("click", () => {
      row.remove();
      refreshSaveButton();
    });

    row.append(nameLabel, idLabel, removeButton);
    membersContainer.append(row);
    refreshSaveButton();
  }

  function refreshSaveButton() {
    const count = membersContainer.querySelectorAll(".scan-member").length;
    saveButton.disabled = count === 0;
  }

  function findIdCandidates(text) {
    const candidates = new Set();
    for (const match of text.matchAll(/(?:\d[\s.-]*){16}/g)) {
      const digits = match[0].replace(/\D/g, "");
      if (digits.length === 16) candidates.add(digits);
    }
    return [...candidates];
  }

  function identifyFamilyCardNumber(text, candidates) {
    for (const line of text.split(/\r?\n/)) {
      if (!/(?:\bNO(?:MOR)?\.?\s*(?:KARTU\s*KELUARGA|KK)\b|\bKARTU\s*KELUARGA\b)/i.test(line)) {
        continue;
      }
      const number = findIdCandidates(line)[0];
      if (number) return number;
    }
    return candidates.length === 1 ? candidates[0] : "";
  }

  function setMembers(ids) {
    membersContainer.replaceChildren();
    ids.slice(0, 20).forEach((id) => addMember(id));
    if (ids.length > 20) {
      scanStatus.textContent =
        "Lebih dari 20 nomor terdeteksi. Periksa teks OCR dan tambahkan anggota yang diperlukan secara manual.";
    }
    refreshSaveButton();
  }

  imageInput.addEventListener("change", () => {
    selectedImage = imageInput.files?.[0] ?? null;
    scanButton.disabled = !selectedImage;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    if (!selectedImage) {
      preview.hidden = true;
      return;
    }
    if (!selectedImage.type.startsWith("image/") || selectedImage.size > 10 * 1024 * 1024) {
      selectedImage = null;
      imageInput.value = "";
      scanButton.disabled = true;
      preview.hidden = true;
      scanStatus.textContent = "Pilih file gambar berukuran maksimal 10 MB.";
      return;
    }
    previewUrl = URL.createObjectURL(selectedImage);
    preview.src = previewUrl;
    preview.hidden = false;
    scanStatus.textContent = "Gambar siap dipindai secara lokal di browser.";
  });

  addMemberButton.addEventListener("click", () => addMember());

  scanButton.addEventListener("click", async () => {
    if (!selectedImage) return;
    if (!window.Tesseract) {
      scanStatus.textContent =
        "Modul OCR tidak berhasil dimuat. Periksa koneksi internet lalu muat ulang halaman.";
      return;
    }

    scanButton.disabled = true;
    saveButton.disabled = true;
    scanStatus.textContent = "Menyiapkan OCR Bahasa Indonesia…";
    ocrText.textContent = "";
    try {
      const worker = await window.Tesseract.createWorker("ind", 1, {
        logger(message) {
          if (message.status === "recognizing text") {
            scanStatus.textContent = `Memindai KK… ${Math.round(message.progress * 100)}%`;
          } else if (message.status === "loading language traineddata") {
            scanStatus.textContent = "Mengunduh data OCR Bahasa Indonesia…";
          }
        },
      });
      try {
        const result = await worker.recognize(selectedImage);
        const text = result.data.text ?? "";
        const ids = findIdCandidates(text);
        const familyId = identifyFamilyCardNumber(text, ids);
        const memberIds = ids.filter((id) => id !== familyId);
        kkNumber.value = familyId;
        ocrText.textContent = text || "Tidak ada teks yang terbaca.";
        setMembers(memberIds);
        if (memberIds.length === 0) {
          scanStatus.textContent =
            "NIK belum terdeteksi. Perbaiki pencahayaan/fokus atau tambahkan anggota secara manual.";
        } else {
          scanStatus.textContent =
            `${memberIds.length} kemungkinan NIK terdeteksi. Periksa nomor KK, NIK, dan isi nama anggota sebelum menyimpan.`;
        }
      } finally {
        await worker.terminate();
      }
    } catch (error) {
      console.error("Pemindaian Kartu Keluarga gagal.", error);
      scanStatus.textContent =
        "Pemindaian gagal. Coba foto yang lebih jelas atau tambahkan anggota secara manual.";
    } finally {
      scanButton.disabled = !selectedImage;
    }
  });

  document.getElementById("kk-import-form").addEventListener("submit", (event) => {
    const rows = [...membersContainer.querySelectorAll(".scan-member")];
    if (rows.length === 0) {
      event.preventDefault();
      scanStatus.textContent = "Tambahkan setidaknya satu anggota keluarga.";
      return;
    }
    const ids = rows.map((row) => row.querySelector('[name="national_id"]').value.trim());
    if (new Set(ids).size !== ids.length) {
      event.preventDefault();
      scanStatus.textContent = "Ada NIK duplikat. Periksa daftar sebelum menyimpan.";
    }
  });
})();
