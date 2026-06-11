document.addEventListener("DOMContentLoaded", () => {
  initMultiDateWidgets();
  initScheduleCalculations();
});

function initMultiDateWidgets() {
  const widgets = document.querySelectorAll(".multi-date-widget");

  const formatDate = (value) => {
    try {
      return new Date(`${value}T00:00:00`).toLocaleDateString("es-CO", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      });
    } catch (error) {
      return value;
    }
  };

  widgets.forEach((widget) => {
    const hiddenInput = widget.querySelector('input[type="hidden"]');
    const picker = widget.querySelector(".multi-date-picker");
    const addButton = widget.querySelector(".multi-date-add");
    const list = widget.querySelector(".multi-date-list");

    const getValues = () =>
      hiddenInput.value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean)
        .sort();

    const setValues = (values) => {
      hiddenInput.value = values.join(",");
      render(values);
      widget.dispatchEvent(
        new CustomEvent("multi-date-change", {
          detail: { values },
        }),
      );
    };

    const render = (values) => {
      list.innerHTML = "";
      values.forEach((value) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "multi-date-chip";
        chip.dataset.value = value;
        chip.textContent = formatDate(value);
        chip.addEventListener("click", () => {
          setValues(getValues().filter((item) => item !== value));
        });
        list.appendChild(chip);
      });
    };

    addButton.addEventListener("click", () => {
      const nextValue = picker.value;
      if (!nextValue) {
        picker.focus();
        return;
      }

      picker.setCustomValidity("");
      const values = Array.from(new Set([...getValues(), nextValue])).sort();
      setValues(values);
      picker.value = "";
    });

    render(getValues());
  });
}

function initScheduleCalculations() {
  const shiftMetricsNode = document.getElementById("shift-metrics-data");
  const scheduleTable = document.querySelector(".schedule-table");
  if (!shiftMetricsNode || !scheduleTable) {
    return;
  }

  const shiftMetrics = JSON.parse(shiftMetricsNode.textContent);
  const nightStart = scheduleTable.dataset.nightStart || "19:00";
  const defaultWeeklyHours = parseFloat(scheduleTable.dataset.defaultWeeklyHours || "0");
  const defaultDailyMax = parseFloat(scheduleTable.dataset.defaultDailyMax || "0");
  const showNightHours = scheduleTable.dataset.showNightHours === "true";
  const showDetailedAlerts = scheduleTable.dataset.showDetailedAlerts === "true";
  const scheduleClosed = scheduleTable.dataset.scheduleClosed === "true";

  const parseDecimal = (value) => {
    const normalized = String(value ?? "")
      .trim()
      .replace(",", ".");
    const parsed = Number.parseFloat(normalized);
    return Number.isFinite(parsed) ? parsed : 0;
  };

  const formatHours = (value, suffix = false) => {
    const rounded = Math.round((value + Number.EPSILON) * 100) / 100;
    const formatted = Number.isInteger(rounded) ? String(rounded) : String(rounded).replace(/\.?0+$/, "");
    return suffix ? `${formatted} h` : formatted;
  };

  const formatBalance = (value) => {
    if (Math.abs(value) < 0.005) {
      return formatHours(0);
    }
    if (value < 0) {
      return `-${formatHours(Math.abs(value))}`;
    }
    return formatHours(value);
  };

  const toMinutes = (timeValue) => {
    const [hours, minutes] = timeValue.split(":").map((item) => Number.parseInt(item, 10));
    return hours * 60 + minutes;
  };

  const setSelectValue = (select, desiredValue) => {
    if (!select) {
      return;
    }
    const options = Array.from(select.options);
    const exact = options.find((option) => option.value === desiredValue);
    if (exact) {
      select.value = exact.value;
      return;
    }
    const normalized = String(desiredValue || "").trim().toLowerCase();
    const caseInsensitive = options.find((option) => option.value.trim().toLowerCase() === normalized);
    if (caseInsensitive) {
      select.value = caseInsensitive.value;
    }
  };

  const parseRangeMetrics = (label) => {
    const match = String(label || "").trim().match(/^(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$/);
    if (!match) {
      return { hours: 0, night_hours: 0 };
    }

    let startMinutes = toMinutes(match[1]);
    let endMinutes = toMinutes(match[2]);
    if (endMinutes <= startMinutes) {
      endMinutes += 24 * 60;
    }

    const totalHours = (endMinutes - startMinutes) / 60;
    const nightStartMinutes = toMinutes(nightStart);
    const nightHours = endMinutes <= nightStartMinutes
      ? 0
      : Math.max(endMinutes - Math.max(startMinutes, nightStartMinutes), 0) / 60;

    return {
      hours: totalHours,
      night_hours: nightHours,
    };
  };

  const getShiftMetrics = (label) => {
    const normalized = String(label || "").trim();
    if (!normalized) {
      return { hours: 0, night_hours: 0 };
    }

    if (Object.prototype.hasOwnProperty.call(shiftMetrics, normalized)) {
      return {
        hours: parseDecimal(shiftMetrics[normalized].hours),
        night_hours: parseDecimal(shiftMetrics[normalized].night_hours),
      };
    }

    return parseRangeMetrics(normalized);
  };

  const rows = document.querySelectorAll(".schedule-row");
  rows.forEach((row) => {
    const weeklyTarget = parseDecimal(row.dataset.weeklyTarget);
    const dailyMax = parseDecimal(row.dataset.dailyMax);
    const priorDayBalance = Math.max(parseDecimal(row.dataset.priorDayBalance), 0);
    const priorHourBalance = Math.max(parseDecimal(row.dataset.priorHourBalance), 0);
    const priorTotalBalance = parseDecimal(row.dataset.priorTotalBalance);
    const dayReferenceHours = parseDecimal(row.dataset.dayReferenceHours);
    const effectiveWeeklyTarget = weeklyTarget > 0 ? weeklyTarget : defaultWeeklyHours;
    const effectiveDailyMax = dailyMax > 0 ? dailyMax : defaultDailyMax;
    const totalCell = row.querySelector("[data-total-hours]");
    const overtimeCell = row.querySelector("[data-overtime-hours]");
    const nightCell = row.querySelector("[data-night-hours]");
    const deltaCell = row.querySelector("[data-pending-variance]");
    const summaryCell = row.querySelector("[data-live-summary]");
    const balanceNote = row.querySelector("[data-balance-note]");
    const pendingDatesInput = row.querySelector('input[name$="-pending_dates_note"]');
    const pendingDaysInput = row.querySelector('input[name$="-pending_days"]');
    const pendingHoursInput = row.querySelector('input[name$="-pending_hours"]');
    const dayCells = row.querySelectorAll("[data-day-index]");

    const getPendingDatesCount = () =>
      (pendingDatesInput?.value || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean).length;

    const syncPendingDaysFromDates = (force = false) => {
      if (!pendingDaysInput) {
        return;
      }
      const pendingDatesCount = getPendingDatesCount();
      if (force || pendingDatesCount > 0 || parseDecimal(pendingDaysInput.value) > 0) {
        pendingDaysInput.value = String(pendingDatesCount);
      }
    };

    const updateCompensationControl = (modeSelect, hoursInput) => {
      if (!modeSelect) {
        return;
      }
      const paymentBlock = modeSelect.closest(".day-payment");
      const hoursWrap = paymentBlock?.querySelector("[data-pay-hours-wrap]");
      const isPayHours = modeSelect.value === "pay_hours";
      if (hoursWrap) {
        hoursWrap.hidden = !isPayHours;
      }
      if (hoursInput) {
        hoursInput.required = isPayHours;
      }
    };

    const updateBalanceNote = (paymentDaysUsed, paymentHoursUsed) => {
      if (!balanceNote) {
        return;
      }

      const remainingDayBalance = Math.max(priorDayBalance - paymentDaysUsed, 0);
      const remainingDayEquivalentHours = remainingDayBalance * dayReferenceHours;
      const remainingHourBalance = Math.max(priorHourBalance - paymentHoursUsed, 0);

      balanceNote.textContent = `Saldo previo: ${formatHours(priorDayBalance)} dia(s) (${formatHours(priorDayBalance * dayReferenceHours)} h) y ${formatHours(priorHourBalance)} h. Disponible: ${formatHours(remainingDayBalance)} dia(s) (${formatHours(remainingDayEquivalentHours)} h) y ${formatHours(remainingHourBalance)} h.`;
    };

    const updatePaymentInfo = (dayCell, modeValue, state) => {
      const paymentInfo = dayCell.querySelector("[data-payment-info]");
      if (!paymentInfo) {
        return;
      }

      const remainingDayBalance = Math.max(priorDayBalance - state.paymentDaysUsed, 0);
      const remainingHourBalance = Math.max(priorHourBalance - state.paymentHoursUsed, 0);

      if (modeValue === "pay_day") {
        paymentInfo.hidden = false;
        if (priorDayBalance > 0.001) {
          paymentInfo.textContent = `Pago dia: descuenta 1 dia pendiente = ${formatHours(dayReferenceHours)} h. Restan ${formatHours(remainingDayBalance)} dia(s).`;
        } else {
          paymentInfo.textContent = "Sin dias pendientes disponibles.";
        }
        return;
      }

      if (modeValue === "pay_hours") {
        paymentInfo.hidden = false;
        const coveredHours = state.dailyHours + state.compensationHoursValue;
        if (priorHourBalance <= 0.001) {
          paymentInfo.textContent = "Sin horas pendientes disponibles.";
        } else if (coveredHours > dayReferenceHours + 0.001) {
          paymentInfo.textContent = `Trabajadas ${formatHours(state.dailyHours)} h + pagas ${formatHours(state.compensationHoursValue)} h = ${formatHours(coveredHours)} h. Supera la jornada de ${formatHours(dayReferenceHours)} h.`;
        } else if (coveredHours >= dayReferenceHours - 0.001) {
          paymentInfo.textContent = `Trabajadas ${formatHours(state.dailyHours)} h + pagas ${formatHours(state.compensationHoursValue)} h = ${formatHours(coveredHours)} h. Jornada cubierta. Restan ${formatHours(remainingHourBalance)} h pendientes.`;
        } else {
          paymentInfo.textContent = `Trabajadas ${formatHours(state.dailyHours)} h + pagas ${formatHours(state.compensationHoursValue)} h = ${formatHours(coveredHours)} h. Faltan ${formatHours(dayReferenceHours - coveredHours)} h para completar la jornada.`;
        }
        return;
      }

      paymentInfo.hidden = true;
      paymentInfo.textContent = "";
    };

    const buildLiveSummary = (summaryState) => {
      if (showDetailedAlerts) {
        const liveMessages = [];

        if (showNightHours && summaryState.totalNightHours > 0.001) {
          liveMessages.push(`Recargo nocturno acumulado: ${formatHours(summaryState.totalNightHours, true)}.`);
        }
        if (summaryState.overtimeHours > 0.001) {
          liveMessages.push(`Extras calculadas: ${formatHours(summaryState.overtimeHours, true)}.`);
        }
        if (summaryState.daysOverLimit > 0) {
          liveMessages.push(`${summaryState.daysOverLimit} dia(s) supera(n) el maximo diario.`);
        }
        if (summaryState.pendingDatesCount !== Math.round(summaryState.pendingDays)) {
          liveMessages.push("Fechas pendientes y dias pendientes deben coincidir.");
        }
        if (summaryState.paymentDaysUsed > priorDayBalance + 0.001) {
          liveMessages.push("No hay suficientes dias pendientes de horarios anteriores para aplicar pago dia.");
        }
        if (summaryState.paymentHoursUsed > priorHourBalance + 0.001) {
          liveMessages.push("Las horas marcadas como pago superan el saldo previo disponible.");
        }
        if (summaryState.invalidPayHoursCount > 0) {
          liveMessages.push("Pago horas requiere una cantidad mayor que cero.");
        }
        if (summaryState.payHoursOverTargetCount > 0) {
          liveMessages.push("Hay dias donde las horas pagas superan la jornada del dia.");
        }
        if (summaryState.payHoursIncompleteCount > 0) {
          liveMessages.push("Hay dias con pago horas que aun no completan la jornada.");
        }
        if (summaryState.balance < -0.005) {
          liveMessages.push("El saldo queda negativo porque se aplico mas pago del acumulado.");
        }

        return liveMessages;
      }

      const conciseMessages = [];
      if (summaryState.overtimeHours > 0.001) {
        conciseMessages.push(`Extras: ${formatHours(summaryState.overtimeHours, true)}.`);
      }
      if (summaryState.daysOverLimit > 0) {
        conciseMessages.push("Revisa horas del turno.");
      }
      if (
        summaryState.pendingDatesCount !== Math.round(summaryState.pendingDays)
        || summaryState.paymentDaysUsed > priorDayBalance + 0.001
        || summaryState.paymentHoursUsed > priorHourBalance + 0.001
        || summaryState.invalidPayHoursCount > 0
        || summaryState.payHoursOverTargetCount > 0
        || summaryState.balance < -0.005
      ) {
        conciseMessages.push("Revisa pendientes o saldo.");
      }
      if (summaryState.payHoursIncompleteCount > 0) {
        conciseMessages.push("Hay jornadas incompletas con pago horas.");
      }
      return conciseMessages;
    };

    const recalculateRow = () => {
      let totalHours = 0;
      let totalNightHours = 0;
      let daysOverLimit = 0;
      let paymentDaysUsed = 0;
      let paymentHoursUsed = 0;
      let invalidPayHoursCount = 0;
      let payHoursOverTargetCount = 0;
      let payHoursIncompleteCount = 0;

      dayCells.forEach((dayCell) => {
        const dayIndex = dayCell.dataset.dayIndex;
        const shift1Select = row.querySelector(`[name$="-day_${dayIndex}_shift_1"]`);
        const shift2Select = row.querySelector(`[name$="-day_${dayIndex}_shift_2"]`);
        const compensationMode = row.querySelector(`[name$="-day_${dayIndex}_compensation_mode"]`);
        const compensationHours = row.querySelector(`[name$="-day_${dayIndex}_compensation_hours"]`);
        const modeValue = compensationMode?.value || "";

        if (!scheduleClosed && modeValue === "pay_day") {
          setSelectValue(shift1Select, "descanso");
          setSelectValue(shift2Select, "");
          if (compensationHours) {
            compensationHours.value = "";
          }
        }

        const shift1 = shift1Select?.value || "";
        const shift2 = shift2Select?.value || "";
        const shift1Metrics = getShiftMetrics(shift1);
        const shift2Metrics = getShiftMetrics(shift2);
        const dailyHours = shift1Metrics.hours + shift2Metrics.hours;
        const dailyNightHours = shift1Metrics.night_hours + shift2Metrics.night_hours;
        const compensationHoursValue = parseDecimal(compensationHours?.value);
        const compensatedDayHours = dailyHours + (modeValue === "pay_hours" ? compensationHoursValue : 0);

        totalHours += dailyHours;
        totalNightHours += dailyNightHours;
        updateCompensationControl(compensationMode, compensationHours);

        if (modeValue === "pay_day") {
          paymentDaysUsed += 1;
        }

        if (modeValue === "pay_hours") {
          paymentHoursUsed += compensationHoursValue;
          if (compensationHoursValue <= 0.001) {
            invalidPayHoursCount += 1;
          } else if (compensatedDayHours > dayReferenceHours + 0.001) {
            payHoursOverTargetCount += 1;
          } else if (compensatedDayHours < dayReferenceHours - 0.001) {
            payHoursIncompleteCount += 1;
          }
        }

        const dayHours = dayCell.querySelector("[data-day-hours]");
        const dayNight = dayCell.querySelector("[data-day-night]");
        if (dayHours) {
          dayHours.textContent = formatHours(dailyHours, true);
          dayHours.classList.toggle("is-over-limit", effectiveDailyMax > 0 && dailyHours > effectiveDailyMax + 0.001);
        }

        if (dayNight && showNightHours) {
          if (dailyNightHours > 0.001) {
            dayNight.hidden = false;
            dayNight.textContent = `Rec. noct. ${formatHours(dailyNightHours, true)}`;
            dayNight.classList.add("has-night");
          } else {
            dayNight.hidden = true;
            dayNight.textContent = "";
            dayNight.classList.remove("has-night");
          }
        } else if (dayNight) {
          dayNight.hidden = true;
          dayNight.textContent = "";
          dayNight.classList.remove("has-night");
        }

        const isOverLimit = effectiveDailyMax > 0 && dailyHours > effectiveDailyMax + 0.001;
        dayCell.classList.toggle("is-over-limit", isOverLimit);
        if (isOverLimit) {
          daysOverLimit += 1;
        }

      });

      updateBalanceNote(paymentDaysUsed, paymentHoursUsed);
      dayCells.forEach((dayCell) => {
        const dayIndex = dayCell.dataset.dayIndex;
        const shift1 = row.querySelector(`[name$="-day_${dayIndex}_shift_1"]`)?.value || "";
        const shift2 = row.querySelector(`[name$="-day_${dayIndex}_shift_2"]`)?.value || "";
        const modeValue = row.querySelector(`[name$="-day_${dayIndex}_compensation_mode"]`)?.value || "";
        const compensationHoursValue = parseDecimal(row.querySelector(`[name$="-day_${dayIndex}_compensation_hours"]`)?.value);
        const shift1Metrics = getShiftMetrics(shift1);
        const shift2Metrics = getShiftMetrics(shift2);
        updatePaymentInfo(dayCell, modeValue, {
          paymentDaysUsed,
          paymentHoursUsed,
          dailyHours: shift1Metrics.hours + shift2Metrics.hours,
          compensationHoursValue,
        });
      });

      const overtimeHours = Math.max(totalHours - effectiveWeeklyTarget, 0);
      const pendingDatesCount = getPendingDatesCount();
      const pendingDays = parseDecimal(pendingDaysInput?.value);
      const pendingHours = parseDecimal(pendingHoursInput?.value);
      const balance =
        priorTotalBalance
        + (pendingDays * dayReferenceHours)
        + pendingHours
        + overtimeHours
        - (paymentDaysUsed * dayReferenceHours)
        - paymentHoursUsed;

      if (totalCell) {
        totalCell.textContent = formatHours(totalHours);
      }
      if (overtimeCell) {
        overtimeCell.textContent = formatHours(overtimeHours);
      }
      if (nightCell) {
        nightCell.textContent = formatHours(totalNightHours);
      }
      if (deltaCell) {
        deltaCell.textContent = formatBalance(balance);
        deltaCell.classList.toggle("is-positive", balance > 0.005);
        deltaCell.classList.toggle("is-negative", balance < -0.005);
      }

      const liveMessages = buildLiveSummary({
        totalNightHours,
        overtimeHours,
        daysOverLimit,
        pendingDatesCount,
        pendingDays,
        paymentDaysUsed,
        paymentHoursUsed,
        invalidPayHoursCount,
        payHoursOverTargetCount,
        payHoursIncompleteCount,
        balance,
      });

      if (summaryCell) {
        summaryCell.textContent = liveMessages.join(" ");
        summaryCell.hidden = liveMessages.length === 0;
      }
    };

    if (!scheduleClosed) {
      row.querySelectorAll("select").forEach((field) => {
        field.addEventListener("change", recalculateRow);
      });

      row.querySelectorAll('input[name*="_compensation_hours"]').forEach((field) => {
        field.addEventListener("input", recalculateRow);
        field.addEventListener("change", recalculateRow);
      });

      if (pendingDaysInput) {
        pendingDaysInput.addEventListener("input", recalculateRow);
        pendingDaysInput.addEventListener("change", recalculateRow);
      }

      if (pendingHoursInput) {
        pendingHoursInput.addEventListener("input", recalculateRow);
        pendingHoursInput.addEventListener("change", recalculateRow);
      }

      pendingDatesInput?.closest(".multi-date-widget")?.addEventListener("multi-date-change", () => {
        syncPendingDaysFromDates(true);
        recalculateRow();
      });
    }

    syncPendingDaysFromDates(false);
    recalculateRow();
  });
}
