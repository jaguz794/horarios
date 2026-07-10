document.addEventListener("DOMContentLoaded", () => {
  initScheduleCalculations();
});

function initScheduleCalculations() {
  const shiftMetricsNode = document.getElementById("shift-metrics-data");
  const scheduleTable = document.querySelector(".schedule-table");
  const scheduleForm = document.querySelector('[data-schedule-form="true"]');
  if (!shiftMetricsNode || !scheduleTable || !scheduleForm) {
    return;
  }

  const shiftMetrics = JSON.parse(shiftMetricsNode.textContent);
  const nightStart = scheduleTable.dataset.nightStart || "19:00";
  const defaultWeeklyHours = parseFloat(scheduleTable.dataset.defaultWeeklyHours || "0");
  const defaultDailyMax = parseFloat(scheduleTable.dataset.defaultDailyMax || "0");
  const showNightHours = scheduleTable.dataset.showNightHours === "true";
  const showDetailedAlerts = scheduleTable.dataset.showDetailedAlerts === "true";
  const scheduleClosed = scheduleTable.dataset.scheduleClosed === "true";
  const moneyHourModes = new Set(["pay_money", "pay_money_hours"]);
  const moneyDayModes = new Set(["pay_money_day"]);
  const advanceDayModes = new Set(["advance_day"]);
  const maxAdvancePendingDays = 2;
  const compensationModesWithHours = new Set(["pay_hours", ...moneyHourModes]);
  const restShiftLabels = new Set(["descanso"]);
  const leaveShiftLabels = new Set(["incapacidad", "traslado", "vacaciones", "renuncia", "licencia"]);
  const autosaveEnabled = scheduleForm.dataset.autosaveEnabled === "true";
  const csrfToken = scheduleForm.querySelector('input[name="csrfmiddlewaretoken"]')?.value || "";
  let autosaveDirty = false;
  let autosaveInFlight = false;
  let manualSubmitInProgress = false;
  let lastInteractionAt = Date.now();

  const parseDecimal = (value) => {
    const normalized = String(value ?? "").trim().replace(",", ".");
    const parsed = Number.parseFloat(normalized);
    return Number.isFinite(parsed) ? parsed : 0;
  };

  const roundHours = (value) => Math.round((Number(value) + Number.EPSILON) * 100) / 100;
  const roundInt = (value) => Math.round(Number(value) + Number.EPSILON);

  const formatHours = (value, suffix = false) => {
    const rounded = roundHours(value);
    const formatted = Number.isInteger(rounded) ? String(rounded) : String(rounded).replace(/\.?0+$/, "");
    return suffix ? `${formatted} h` : formatted;
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

  const getShiftNonWorkCategory = (label) => {
    const normalized = String(label || "").trim().toLowerCase();
    if (!normalized) {
      return "";
    }
    if (restShiftLabels.has(normalized)) {
      return "rest";
    }
    if (leaveShiftLabels.has(normalized)) {
      return "leave";
    }
    return "";
  };

  const getWeeklyRestDayIndex = (dayStates) => {
    const sundayState = dayStates.find((dayState) => dayState.dayIndex === 0);
    if (!sundayState || sundayState.dailyHours <= 0.001) {
      return 0;
    }

    for (const dayState of dayStates) {
      if (dayState.dayIndex === 0) {
        continue;
      }
      if (dayState.dailyHours > 0.001) {
        continue;
      }
      if (dayState.shiftCategories.has("rest")
        || dayState.modeValue === "pay_day"
        || advanceDayModes.has(dayState.modeValue)) {
        return dayState.dayIndex;
      }
    }

    return 0;
  };

  const calculateWeeklyHourMetrics = (totalWorkedHours, expectedWeeklyHours, weeklyTargetHours) => {
    const normalizedWorkedHours = roundHours(totalWorkedHours);
    const normalizedExpectedHours = roundHours(expectedWeeklyHours);
    const normalizedWeeklyTarget = roundHours(weeklyTargetHours);
    const overtimeThresholdHours = normalizedWeeklyTarget > 0
      ? normalizedWeeklyTarget
      : normalizedExpectedHours;
    const missingHours = roundHours(Math.max(normalizedExpectedHours - normalizedWorkedHours, 0));
    const overtimeHours = roundHours(Math.max(normalizedWorkedHours - overtimeThresholdHours, 0));
    const hourBalanceDelta = roundHours(overtimeHours - missingHours);
    const expectedDifference = roundHours(normalizedWorkedHours - normalizedExpectedHours);
    return {
      totalWorkedHours: normalizedWorkedHours,
      expectedWeeklyHours: normalizedExpectedHours,
      overtimeThresholdHours,
      missingHours,
      overtimeHours,
      hourBalanceDelta,
      expectedDifference,
    };
  };

  const canApplyNegativeRestDay = (remainingDayBalance, remainingAdvancePendingBalance) => {
    const nextDayBalance = roundHours(remainingDayBalance - 1);
    const nextPendingBalance = roundHours(remainingAdvancePendingBalance + 1);
    return nextDayBalance >= (maxAdvancePendingDays * -1) - 0.001
      && nextPendingBalance <= maxAdvancePendingDays + 0.001;
  };

  const buildExpectedPlan = (dayStates, weeklyTargetValue) => {
    const mandatoryRestIndex = getWeeklyRestDayIndex(dayStates);
    const expectedIndexes = [];
    const weeklyTarget = roundHours(weeklyTargetValue);
    const provisionalStates = dayStates.map((dayState) => {
      const isHoliday = String(dayState.specialDayLabel || "").includes("Festivo") && dayState.dayIndex !== 0;
      const isUnworkedHoliday = isHoliday && dayState.dailyHours <= 0.001;
      const isLeaveDay =
        dayState.dailyHours <= 0.001
        && dayState.shiftCategories.has("leave")
        && !isUnworkedHoliday
        && dayState.modeValue !== "pay_day"
        && !advanceDayModes.has(dayState.modeValue);
      const isAdditionalRestDay =
        dayState.modeValue === ""
        && dayState.dailyHours <= 0.001
        && dayState.shiftCategories.has("rest")
        && !isUnworkedHoliday
        && !isLeaveDay
        && dayState.dayIndex !== mandatoryRestIndex;

      let expectedReason = "laborable";
      if (dayState.dayIndex === mandatoryRestIndex) {
        expectedReason = "descanso_obligatorio";
      }
      if (isUnworkedHoliday) {
        expectedReason = "festivo";
      }
      if (dayState.modeValue === "pay_day") {
        expectedReason = "descanso_compensatorio";
      }
      if (advanceDayModes.has(dayState.modeValue)) {
        expectedReason = "descanso_adelantado";
      }
      if (isLeaveDay) {
        expectedReason = "novedad_no_laborable";
      }
      if (isAdditionalRestDay) {
        expectedReason = "descanso_adicional";
      }
      if (expectedReason === "laborable") {
        expectedIndexes.push(dayState.dayIndex);
      }

      return {
        ...dayState,
        isHoliday,
        isUnworkedHoliday,
        isLeaveDay,
        isAdditionalRestDay,
        expectedReason,
      };
    });

    const expectedWorkDays = expectedIndexes.length;
    const expectedWeeklyHours = expectedWorkDays > 0
      ? roundHours((weeklyTarget * expectedWorkDays) / 6)
      : 0;
    const totalCents = roundInt(expectedWeeklyHours * 100);
    const baseCents = expectedWorkDays > 0 ? Math.floor(totalCents / expectedWorkDays) : 0;
    const remainderCents = expectedWorkDays > 0 ? totalCents % expectedWorkDays : 0;
    const expectedHoursByIndex = new Map();
    expectedIndexes.forEach((index, position) => {
      const cents = baseCents + (position < remainderCents ? 1 : 0);
      expectedHoursByIndex.set(index, roundHours(cents / 100));
    });

    return {
      mandatoryRestIndex,
      expectedWorkDays,
      expectedWeeklyHours,
      dayStates: provisionalStates.map((dayState) => ({
        ...dayState,
        expectedHours: expectedHoursByIndex.get(dayState.dayIndex) || 0,
      })),
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

  const resolvePaymentUsage = (
    entries,
    availableDayBalance,
    availableHourBalance,
    availableAdvancePendingBalance,
    dayReferenceHoursValue,
    weeklyTargetHoursValue,
  ) => {
    let remainingDayBalance = roundHours(availableDayBalance);
    let remainingHourBalance = roundHours(availableHourBalance);
    let remainingAdvancePendingBalance = Math.max(roundHours(availableAdvancePendingBalance), 0);

    let paymentDaysUsed = 0;
    let advanceRestDaysUsed = 0;
    let paymentDaysFromDayBalance = 0;
    let uncoveredPaymentDays = 0;
    let moneyPaymentDaysUsed = 0;
    let paymentHoursUsed = 0;
    let moneyPaymentHoursUsed = 0;
    const invalidPayDayIndices = [];
    const invalidPayMoneyDayIndices = [];
    const invalidPayHoursIndices = [];
    const invalidPayMoneyIndices = [];
    const invalidAdvanceDayLimitIndices = [];
    const invalidAdvanceDayWithBalanceIndices = [];
    const invalidAutoRestDayIndices = [];
    const dayStates = {};

    [...entries]
      .sort((left, right) => left.index - right.index)
      .forEach((entry) => {
        const requestedHours = roundHours(entry.hours);
        const workedHours = roundHours(entry.workedHours);
        const specialGenerated = Boolean(entry.specialGenerated);
        const dayState = {
          mode: entry.mode,
          requestedHours,
          source: "",
          valid: true,
          availableDayBalance: roundHours(Math.max(remainingDayBalance, 0)),
          availableHourBalance: roundHours(Math.max(remainingHourBalance, 0)),
          availableAdvancePendingBalance: roundHours(remainingAdvancePendingBalance),
          remainingDayBalance,
          remainingHourBalance,
          remainingAdvancePendingBalance,
          generatedDay: false,
          appliedHours: 0,
          dayMovementType: "",
          hoursMovementType: "",
        };

        if (entry.mode === "pay_day" || entry.isAdditionalRestDay) {
          if (remainingDayBalance + 0.001 >= 1) {
            remainingDayBalance = roundHours(remainingDayBalance - 1);
            paymentDaysFromDayBalance += 1;
            paymentDaysUsed += 1;
            dayState.source = "day_balance";
            dayState.dayMovementType = "pay_day";
          } else if (canApplyNegativeRestDay(remainingDayBalance, remainingAdvancePendingBalance)) {
            remainingDayBalance = roundHours(remainingDayBalance - 1);
            remainingAdvancePendingBalance = roundHours(remainingAdvancePendingBalance + 1);
            advanceRestDaysUsed += 1;
            dayState.source = "advance_rest";
            dayState.dayMovementType = "advance_day";
          } else {
            if (entry.mode === "pay_day") {
              uncoveredPaymentDays += 1;
              invalidPayDayIndices.push(entry.index);
            } else {
              invalidAutoRestDayIndices.push(entry.index);
            }
            dayState.source = "advance_limit";
            dayState.valid = false;
          }
        } else if (moneyDayModes.has(entry.mode)) {
          if (remainingDayBalance + 0.001 >= 1) {
            remainingDayBalance = roundHours(remainingDayBalance - 1);
            moneyPaymentDaysUsed += 1;
            dayState.source = "day_balance";
            dayState.dayMovementType = "pay_money_day";
          } else {
            invalidPayMoneyDayIndices.push(entry.index);
            dayState.source = "insufficient";
            dayState.valid = false;
          }
        } else if (advanceDayModes.has(entry.mode)) {
          if (remainingDayBalance >= 1) {
            invalidAdvanceDayWithBalanceIndices.push(entry.index);
            dayState.source = "use_pay_day";
            dayState.valid = false;
          } else if (remainingAdvancePendingBalance + 1 > maxAdvancePendingDays + 0.001) {
            invalidAdvanceDayLimitIndices.push(entry.index);
            dayState.source = "advance_limit";
            dayState.valid = false;
          } else {
            advanceRestDaysUsed += 1;
            remainingDayBalance = roundHours(remainingDayBalance - 1);
            remainingAdvancePendingBalance = roundHours(remainingAdvancePendingBalance + 1);
            dayState.source = "advance_rest";
            dayState.dayMovementType = "advance_day";
          }
        } else if (entry.mode === "pay_hours") {
          const remainingBefore = remainingHourBalance;
          if (requestedHours <= 0.001 || remainingBefore + 0.001 < requestedHours) {
            invalidPayHoursIndices.push(entry.index);
            dayState.valid = false;
            dayState.source = "insufficient";
          } else {
            paymentHoursUsed = roundHours(paymentHoursUsed + requestedHours);
            remainingHourBalance = roundHours(remainingHourBalance - requestedHours);
            dayState.source = "hour_balance";
            dayState.hoursMovementType = "pay_hours";
            dayState.appliedHours = requestedHours;
          }
        } else if (moneyHourModes.has(entry.mode)) {
          const remainingBefore = remainingHourBalance;
          if (requestedHours <= 0.001 || remainingBefore + 0.001 < requestedHours) {
            invalidPayMoneyIndices.push(entry.index);
            dayState.valid = false;
            dayState.source = "insufficient";
          } else {
            moneyPaymentHoursUsed = roundHours(moneyPaymentHoursUsed + requestedHours);
            remainingHourBalance = roundHours(remainingHourBalance - requestedHours);
            dayState.source = "hour_balance";
            dayState.hoursMovementType = "pay_money_hours";
            dayState.appliedHours = requestedHours;
          }
        }

        if (specialGenerated && workedHours > 0.001) {
          remainingDayBalance = roundHours(remainingDayBalance + 1);
          dayState.generatedDay = true;
          if (remainingAdvancePendingBalance > 0.001) {
            const offsetDays = Math.min(remainingAdvancePendingBalance, 1);
            remainingAdvancePendingBalance = roundHours(remainingAdvancePendingBalance - offsetDays);
          }
        }

        dayState.remainingDayBalance = remainingDayBalance;
        dayState.remainingHourBalance = remainingHourBalance;
        dayState.remainingAdvancePendingBalance = remainingAdvancePendingBalance;
        dayStates[entry.index] = dayState;
      });

    return {
      paymentDaysUsed,
      advanceRestDaysUsed,
      paymentDaysFromDayBalance,
      paymentDaysFromHourBalance: 0,
      uncoveredPaymentDays,
      moneyPaymentDaysUsed,
      paymentDayHourEquivalent: 0,
      paymentHoursUsed,
      moneyPaymentHoursUsed,
      invalidPayDayIndices,
      invalidPayMoneyDayIndices,
      invalidPayHoursIndices,
      invalidPayMoneyIndices,
      invalidAdvanceDayLimitIndices,
      invalidAdvanceDayWithBalanceIndices,
      invalidAutoRestDayIndices,
      remainingDayBalance,
      remainingHourBalance,
      remainingAdvancePendingBalance,
      dayStates,
    };
  };

  const updateCompensationControl = (modeSelect, hoursInput) => {
    if (!modeSelect) {
      return;
    }
    const paymentBlock = modeSelect.closest(".day-payment");
    const hoursWrap = paymentBlock?.querySelector("[data-pay-hours-wrap]");
    const needsHours = compensationModesWithHours.has(modeSelect.value);
    if (hoursWrap) {
      hoursWrap.hidden = !needsHours;
    }
    if (hoursInput) {
      hoursInput.required = needsHours;
      if (!needsHours) {
        hoursInput.value = "";
      }
    }
  };

  const rows = document.querySelectorAll(".schedule-row");

  const markAutosaveDirty = () => {
    if (!autosaveEnabled || scheduleClosed) {
      return;
    }
    autosaveDirty = true;
    lastInteractionAt = Date.now();
  };

  const autosaveScheduleForm = async () => {
    if (!autosaveEnabled || scheduleClosed || manualSubmitInProgress || autosaveInFlight || !autosaveDirty) {
      return;
    }
    if (Date.now() - lastInteractionAt < 1500) {
      return;
    }

    autosaveInFlight = true;
    try {
      const formData = new FormData(scheduleForm);
      const response = await fetch(scheduleForm.action || window.location.href, {
        method: "POST",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "X-Schedule-Autosave": "true",
          "X-CSRFToken": csrfToken,
          Accept: "application/json",
        },
        body: formData,
        credentials: "same-origin",
      });

      if (!response.ok) {
        return;
      }

      const payload = await response.json();
      if (payload?.ok) {
        autosaveDirty = false;
      }
    } catch (_error) {
      // El autoguardado es silencioso: si falla, se reintentara en el siguiente ciclo.
    } finally {
      autosaveInFlight = false;
    }
  };

  scheduleForm.addEventListener(
    "input",
    () => {
      markAutosaveDirty();
    },
    true,
  );
  scheduleForm.addEventListener(
    "change",
    () => {
      markAutosaveDirty();
    },
    true,
  );
  scheduleForm.addEventListener("submit", () => {
    manualSubmitInProgress = true;
  });

  if (autosaveEnabled && !scheduleClosed) {
    window.setInterval(() => {
      autosaveScheduleForm();
    }, 30000);
  }

  rows.forEach((row) => {
    const weeklyTarget = parseDecimal(row.dataset.weeklyTarget);
    const dailyMax = parseDecimal(row.dataset.dailyMax);
    const priorDayBalance = parseDecimal(row.dataset.priorDayBalance);
    const priorHourBalance = parseDecimal(row.dataset.priorHourBalance);
    const priorAdvancePendingBalance = parseDecimal(row.dataset.priorAdvancePendingBalance);
    const dayReferenceHours = parseDecimal(row.dataset.dayReferenceHours);
    const hasOvertimeRestriction = row.dataset.overtimeRestrictionActive === "true";
    const overtimeRestrictionDailyLimit = parseDecimal(row.dataset.overtimeRestrictionDailyLimit);
    const overtimeRestrictionWeeklyLimit = parseDecimal(row.dataset.overtimeRestrictionWeeklyLimit);
    const effectiveWeeklyTarget = weeklyTarget > 0 ? weeklyTarget : defaultWeeklyHours;
    const effectiveDailyMax = dailyMax > 0 ? dailyMax : defaultDailyMax;
    const totalCell = row.querySelector("[data-total-hours]");
    const overtimeCell = row.querySelector("[data-overtime-hours]");
    const nightCell = row.querySelector("[data-night-hours]");
    const dayBalanceCell = row.querySelector("[data-day-balance]");
    const hourBalanceCell = row.querySelector("[data-hour-balance]");
    const summaryCell = row.querySelector("[data-live-summary]");
    const balanceNote = row.querySelector("[data-balance-note]");
    const manualDayAdjustmentInput = row.querySelector('input[name$="-manual_day_adjustment"]');
    const manualHourAdjustmentInput = row.querySelector('input[name$="-manual_hour_adjustment"]');
    const dayCells = row.querySelectorAll("[data-day-index]");
    const employeeName = row.querySelector(".schedule-cell-employee")?.textContent?.trim() || "Esta persona";

    const confirmInventoryParticipation = (event) => {
      const checkbox = event.currentTarget;
      if (!checkbox.checked) {
        return;
      }
      const dayCell = checkbox.closest("[data-day-index]");
      const dayLabel = dayCell?.dataset.dayLabel || "este dia";
      const confirmed = window.confirm(
        `Esta seguro que ${employeeName} participa en el inventario del ${dayLabel}?`,
      );
      if (!confirmed) {
        checkbox.checked = false;
      }
    };

    const updatePaymentInfo = (dayCell, modeValue, state) => {
      const paymentInfo = dayCell.querySelector("[data-payment-info]");
      if (!paymentInfo) {
        return;
      }

      if (modeValue === "pay_day") {
        paymentInfo.hidden = false;
        if (state.paymentState?.source === "day_balance") {
          paymentInfo.textContent = `Pago dia: usa 1 dia acumulado. Saldo estimado tras este pago: ${formatHours(state.paymentState.remainingDayBalance)} dia(s).`;
        } else if (state.paymentState?.source === "advance_rest") {
          paymentInfo.textContent = `Pago dia: no hay saldo previo disponible y deja 1 dia a favor de la empresa. Pendientes por cruzar: ${formatHours(state.paymentState.remainingAdvancePendingBalance)} dia(s).`;
        } else {
          paymentInfo.textContent = "Pago dia: requiere 1 dia acumulado disponible.";
        }
        return;
      }

      if (moneyDayModes.has(modeValue)) {
        paymentInfo.hidden = false;
        if (state.paymentState?.source === "day_balance") {
          paymentInfo.textContent = `Pago en dinero por dia: descuenta 1 dia acumulado. Saldo estimado tras este pago: ${formatHours(state.paymentState.remainingDayBalance)} dia(s).`;
        } else {
          paymentInfo.textContent = "Pago en dinero por dia: requiere 1 dia acumulado disponible.";
        }
        return;
      }

      if (advanceDayModes.has(modeValue)) {
        paymentInfo.hidden = false;
        if (state.paymentState?.source === "advance_rest") {
          paymentInfo.textContent = `Descanso adelantado: deja 1 dia a favor de la empresa. Pendientes por cruzar: ${formatHours(state.paymentState.remainingAdvancePendingBalance)} dia(s).`;
        } else if (state.paymentState?.source === "advance_limit") {
          paymentInfo.textContent = `Limite maximo: ya tiene ${maxAdvancePendingDays} dia(s) adelantado(s) a favor de la empresa.`;
        } else {
          paymentInfo.textContent = "Descanso adelantado: si ya hay dias positivos, usa pago dia.";
        }
        return;
      }

      if (modeValue === "pay_hours") {
        paymentInfo.hidden = false;
        const coveredHours = state.dailyHours + state.compensationHoursValue;
        const expectedHours = state.expectedHours || 0;
        if (coveredHours >= expectedHours - 0.001) {
          paymentInfo.textContent = `Trabajadas ${formatHours(state.dailyHours)} h + pagas ${formatHours(state.compensationHoursValue)} h = jornada cubierta.`;
        } else {
          paymentInfo.textContent = `Trabajadas ${formatHours(state.dailyHours)} h + pagas ${formatHours(state.compensationHoursValue)} h. Faltan ${formatHours(expectedHours - coveredHours)} h.`;
        }
        return;
      }

      if (moneyHourModes.has(modeValue)) {
        paymentInfo.hidden = false;
        paymentInfo.textContent = `Pago en dinero por horas: descuenta ${formatHours(state.compensationHoursValue)} h del saldo acumulado. Saldo estimado tras este pago: ${formatHours(state.paymentState?.remainingHourBalance ?? state.endingHourBalance)} h.`;
        return;
      }

      paymentInfo.hidden = true;
      paymentInfo.textContent = "";
    };

    const updateBalanceNote = (endingDayBalance, endingHourBalance, endingAdvancePendingBalance) => {
      if (!balanceNote) {
        return;
      }
      balanceNote.textContent = `Saldo previo: ${formatHours(priorDayBalance)} dia(s), ${formatHours(priorHourBalance)} h y ${formatHours(priorAdvancePendingBalance)} descanso(s) adelantado(s). Resultado estimado: ${formatHours(endingDayBalance)} dia(s), ${formatHours(endingHourBalance)} h y ${formatHours(endingAdvancePendingBalance)} descanso(s) adelantado(s).`;
    };

    const buildLiveSummary = (summaryState) => {
      const liveMessages = [];

      liveMessages.push(
        `Horas esperadas: ${formatHours(summaryState.expectedWeeklyHours, true)}. Programadas: ${formatHours(summaryState.totalHours, true)}. Diferencia: ${formatHours(summaryState.weeklyHourDifference, true)}.`,
      );
      liveMessages.push(
        `Saldo actual estimado: ${formatHours(summaryState.endingDayBalance)} dia(s) y ${formatHours(summaryState.endingHourBalance)} h.`,
      );
      if (summaryState.specialDaysGenerated > 0.001) {
        liveMessages.push(`Genera ${formatHours(summaryState.specialDaysGenerated)} dia(s) por domingos/festivos.`);
      }
      if (summaryState.overtimeHours > 0.001) {
        liveMessages.push(`Extras calculadas: ${formatHours(summaryState.overtimeHours, true)}.`);
      }
      if (summaryState.weeklyHourDifference < -0.001) {
        liveMessages.push(`Incumplimiento semanal: faltan ${formatHours(Math.abs(summaryState.weeklyHourDifference), true)}.`);
      }
      if (summaryState.overtimeDailyRestrictionExceededCount > 0) {
        liveMessages.push(
          `Restriccion medica diaria: ${summaryState.overtimeDailyRestrictionExceededCount} dia(s) supera(n) ${formatHours(summaryState.overtimeRestrictionDailyLimit, true)} extra(s).`,
        );
      }
      if (summaryState.overtimeWeeklyRestrictionExceeded) {
        liveMessages.push(
          `Restriccion medica semanal: no puede superar ${formatHours(summaryState.overtimeRestrictionWeeklyLimit, true)} extra(s) y esta semana lleva ${formatHours(summaryState.overtimeHours, true)}.`,
        );
      }
      if (showNightHours && summaryState.totalNightHours > 0.001) {
        liveMessages.push(`Recargo nocturno acumulado: ${formatHours(summaryState.totalNightHours, true)}.`);
      }
      if (summaryState.daysOverLimit > 0) {
        liveMessages.push(`${summaryState.daysOverLimit} dia(s) supera(n) el maximo diario.`);
      }
      if (summaryState.invalidPayDayCount > 0) {
        liveMessages.push("Hay descansos sin un dia acumulado disponible.");
      }
      if (summaryState.invalidPayMoneyDayCount > 0) {
        liveMessages.push("Hay pagos en dinero por dia sin un dia acumulado disponible.");
      }
      if (summaryState.invalidAdvanceDayCount > 0) {
        liveMessages.push("Hay descansos adelantados en dias que ya tienen saldo positivo disponible.");
      }
      if (summaryState.invalidAdvanceDayLimitCount > 0 || summaryState.invalidAutoRestDayCount > 0) {
        liveMessages.push(`El trabajador ya alcanzo el limite maximo de ${maxAdvancePendingDays} dias adelantados a favor de la empresa.`);
      }
      if (summaryState.invalidHourDiscountCount > 0) {
        liveMessages.push("Hay descuentos por horas que superan el saldo acumulado disponible.");
      }
      if (summaryState.payHoursOverTargetCount > 0) {
        liveMessages.push("Hay dias donde el pago horas supera la jornada.");
      }
      if (summaryState.payMoneyOverTargetCount > 0) {
        liveMessages.push("Hay pagos en dinero por horas que superan la jornada diaria permitida.");
      }
      if (summaryState.payHoursIncompleteCount > 0) {
        liveMessages.push("Hay dias con pago horas que aun no completan la jornada.");
      }
      if (summaryState.invalidPositiveHoursCount > 0) {
        liveMessages.push("Hay pagos o descuentos por horas con cantidad invalida.");
      }
      if (summaryState.manualDayAdjustment !== 0 || summaryState.manualHourAdjustment !== 0) {
        liveMessages.push(`Ajuste manual aplicado: ${formatHours(summaryState.manualDayAdjustment)} dia(s) y ${formatHours(summaryState.manualHourAdjustment)} h.`);
      }
      if (summaryState.endingAdvancePendingBalance > 0.001) {
        liveMessages.push(`Descansos adelantados pendientes por cruzar: ${formatHours(summaryState.endingAdvancePendingBalance)} dia(s).`);
      }
      if (summaryState.endingDayBalance < -0.001 || summaryState.endingHourBalance < -0.001) {
        liveMessages.push("Parte del saldo queda a favor de la empresa.");
      }

      if (showDetailedAlerts) {
        return liveMessages;
      }

      const conciseMessages = [];
      conciseMessages.push(
        `Esperadas ${formatHours(summaryState.expectedWeeklyHours, true)} / programadas ${formatHours(summaryState.totalHours, true)} / diferencia ${formatHours(summaryState.weeklyHourDifference, true)}.`,
      );
      if (summaryState.specialDaysGenerated > 0.001) {
        conciseMessages.push(`Dia(s) generado(s): ${formatHours(summaryState.specialDaysGenerated)}.`);
      }
      if (summaryState.overtimeHours > 0.001) {
        conciseMessages.push(`Extras: ${formatHours(summaryState.overtimeHours, true)}.`);
      }
      if (summaryState.overtimeDailyRestrictionExceededCount > 0 || summaryState.overtimeWeeklyRestrictionExceeded) {
        conciseMessages.push("Revisa restriccion medica.");
      }
      if (
        summaryState.daysOverLimit > 0
        || summaryState.invalidPayDayCount > 0
        || summaryState.invalidAdvanceDayCount > 0
        || summaryState.invalidAdvanceDayLimitCount > 0
        || summaryState.invalidAutoRestDayCount > 0
        || summaryState.invalidHourDiscountCount > 0
        || summaryState.payHoursOverTargetCount > 0
        || summaryState.payMoneyOverTargetCount > 0
        || summaryState.invalidPositiveHoursCount > 0
      ) {
        conciseMessages.push("Revisa limites o saldo.");
      }
      if (summaryState.endingAdvancePendingBalance > 0.001) {
        conciseMessages.push(`Descansos adelantados: ${formatHours(summaryState.endingAdvancePendingBalance)}.`);
      }
      if (summaryState.endingDayBalance < -0.001 || summaryState.endingHourBalance < -0.001) {
        conciseMessages.push("Saldo a favor de la empresa.");
      }
      return conciseMessages;
    };

    const recalculateRow = () => {
      let totalHours = 0;
      let totalNightHours = 0;
      let daysOverLimit = 0;
      let specialDaysGenerated = 0;
      let payHoursOverTargetCount = 0;
      let payMoneyOverTargetCount = 0;
      let payHoursIncompleteCount = 0;
      let invalidPositiveHoursCount = 0;
      let overtimeDailyRestrictionExceededCount = 0;
      const rawDayStates = [];

      dayCells.forEach((dayCell) => {
        const dayIndex = Number.parseInt(dayCell.dataset.dayIndex || "0", 10);
        const shift1Select = row.querySelector(`[name$="-day_${dayIndex}_shift_1"]`);
        const shift2Select = row.querySelector(`[name$="-day_${dayIndex}_shift_2"]`);
        const compensationMode = row.querySelector(`[name$="-day_${dayIndex}_compensation_mode"]`);
        const compensationHours = row.querySelector(`[name$="-day_${dayIndex}_compensation_hours"]`);
        const modeValue = compensationMode?.value || "";

        if (!scheduleClosed && (modeValue === "pay_day" || advanceDayModes.has(modeValue))) {
          setSelectValue(shift1Select, "descanso");
          setSelectValue(shift2Select, "");
          if (compensationHours) {
            compensationHours.value = "";
          }
        }

        updateCompensationControl(compensationMode, compensationHours);

        const shift1 = shift1Select?.value || "";
        const shift2 = shift2Select?.value || "";
        const shift1Metrics = getShiftMetrics(shift1);
        const shift2Metrics = getShiftMetrics(shift2);
        const dailyHours = roundHours(shift1Metrics.hours + shift2Metrics.hours);
        const dailyNightHours = roundHours(shift1Metrics.night_hours + shift2Metrics.night_hours);
        const compensationHoursValue = roundHours(parseDecimal(compensationHours?.value));
        const specialDayLabel = dayCell.dataset.specialDay || "";
        const specialGenerated = Boolean(specialDayLabel) && dailyHours > 0.001;
        const dailyOvertimeHours = roundHours(Math.max(dailyHours - dayReferenceHours, 0));
        const shiftCategories = new Set([
          getShiftNonWorkCategory(shift1),
          getShiftNonWorkCategory(shift2),
        ].filter(Boolean));

        totalHours = roundHours(totalHours + dailyHours);
        totalNightHours = roundHours(totalNightHours + dailyNightHours);

        if (specialGenerated) {
          specialDaysGenerated += 1;
        }

        if (moneyHourModes.has(modeValue)) {
          if (compensationHoursValue <= 0.001) {
            invalidPositiveHoursCount += 1;
          } else if (compensationHoursValue > dayReferenceHours + 0.001) {
            payMoneyOverTargetCount += 1;
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
        }

        const isOverLimit = effectiveDailyMax > 0 && dailyHours > effectiveDailyMax + 0.001;
        dayCell.classList.toggle("is-over-limit", isOverLimit);
        if (isOverLimit) {
          daysOverLimit += 1;
        }
        if (hasOvertimeRestriction && dailyOvertimeHours > overtimeRestrictionDailyLimit + 0.001) {
          overtimeDailyRestrictionExceededCount += 1;
        }
        rawDayStates.push({
          dayIndex,
          dayCell,
          modeValue,
          compensationHoursValue,
          dailyHours,
          dailyOvertimeHours,
          specialGenerated,
          specialDayLabel,
          shiftCategories,
        });
      });

      const expectedPlan = buildExpectedPlan(rawDayStates, effectiveWeeklyTarget);
      const dayStates = expectedPlan.dayStates;
      dayStates.forEach((dayState) => {
        if (dayState.modeValue === "pay_hours") {
          if (dayState.compensationHoursValue <= 0.001) {
            invalidPositiveHoursCount += 1;
          } else if (dayState.expectedHours <= 0.001) {
            payHoursOverTargetCount += 1;
          } else if (dayState.dailyHours + dayState.compensationHoursValue > dayState.expectedHours + 0.001) {
            payHoursOverTargetCount += 1;
          } else if (dayState.dailyHours + dayState.compensationHoursValue < dayState.expectedHours - 0.001) {
            payHoursIncompleteCount += 1;
          }
        }
      });

      const manualDayAdjustment = roundHours(parseDecimal(manualDayAdjustmentInput?.value));
      const manualHourAdjustment = roundHours(parseDecimal(manualHourAdjustmentInput?.value));
      const weeklyHourMetrics = calculateWeeklyHourMetrics(
        totalHours,
        expectedPlan.expectedWeeklyHours,
        effectiveWeeklyTarget,
      );
      const weeklyHourDifference = weeklyHourMetrics.expectedDifference;
      const overtimeHours = weeklyHourMetrics.overtimeHours;
      const overtimeWeeklyRestrictionExceeded =
        hasOvertimeRestriction && overtimeHours > overtimeRestrictionWeeklyLimit + 0.001;
      const paymentUsage = resolvePaymentUsage(
        dayStates.map((dayState) => ({
          index: dayState.dayIndex,
          mode: dayState.modeValue,
          hours: dayState.compensationHoursValue,
          workedHours: dayState.dailyHours,
          expectedHours: dayState.expectedHours,
          specialGenerated: dayState.specialGenerated,
          isAdditionalRestDay: dayState.isAdditionalRestDay,
        })),
        priorDayBalance + manualDayAdjustment,
        Math.max(priorHourBalance + manualHourAdjustment + weeklyHourMetrics.hourBalanceDelta, 0),
        Math.max(priorAdvancePendingBalance - Math.max(manualDayAdjustment, 0), 0),
        dayReferenceHours,
        effectiveWeeklyTarget,
      );
      const endingDayBalance = roundHours(
        priorDayBalance
        + specialDaysGenerated
        + manualDayAdjustment
        - paymentUsage.paymentDaysUsed
        - paymentUsage.moneyPaymentDaysUsed
        - paymentUsage.advanceRestDaysUsed
      );
      const endingHourBalance = roundHours(
        priorHourBalance
        + weeklyHourMetrics.hourBalanceDelta
        + manualHourAdjustment
        - paymentUsage.paymentHoursUsed
        - paymentUsage.moneyPaymentHoursUsed,
      );
      const endingAdvancePendingBalance = roundHours(paymentUsage.remainingAdvancePendingBalance);

      dayStates.forEach((dayState) => {
        updatePaymentInfo(dayState.dayCell, dayState.modeValue, {
          dailyHours: dayState.dailyHours,
          compensationHoursValue: dayState.compensationHoursValue,
          expectedHours: dayState.expectedHours,
          endingDayBalance,
          endingHourBalance,
          paymentState: paymentUsage.dayStates[dayState.dayIndex],
        });
      });

      if (totalCell) {
        totalCell.textContent = formatHours(totalHours);
      }
      if (overtimeCell) {
        overtimeCell.textContent = formatHours(overtimeHours);
      }
      if (nightCell) {
        nightCell.textContent = formatHours(totalNightHours);
      }
      if (dayBalanceCell) {
        dayBalanceCell.textContent = formatHours(endingDayBalance);
        dayBalanceCell.classList.toggle("metric-cell--negative", endingDayBalance < -0.001);
      }
      if (hourBalanceCell) {
        hourBalanceCell.textContent = formatHours(endingHourBalance);
        hourBalanceCell.classList.toggle("metric-cell--negative", endingHourBalance < -0.001);
      }

      updateBalanceNote(endingDayBalance, endingHourBalance, endingAdvancePendingBalance);

      const liveMessages = buildLiveSummary({
        totalHours,
        expectedWeeklyHours: expectedPlan.expectedWeeklyHours,
        weeklyHourDifference,
        totalNightHours,
        overtimeHours,
        overtimeDailyRestrictionExceededCount,
        overtimeRestrictionDailyLimit,
        overtimeWeeklyRestrictionExceeded,
        overtimeRestrictionWeeklyLimit,
        daysOverLimit,
        specialDaysGenerated,
        invalidPayDayCount: paymentUsage.invalidPayDayIndices.length,
        invalidPayMoneyDayCount: paymentUsage.invalidPayMoneyDayIndices.length,
        invalidAdvanceDayCount: paymentUsage.invalidAdvanceDayWithBalanceIndices.length,
        invalidAdvanceDayLimitCount: paymentUsage.invalidAdvanceDayLimitIndices.length,
        invalidAutoRestDayCount: paymentUsage.invalidAutoRestDayIndices.length,
        invalidHourDiscountCount: paymentUsage.invalidPayHoursIndices.length + paymentUsage.invalidPayMoneyIndices.length,
        payHoursOverTargetCount,
        payMoneyOverTargetCount,
        payHoursIncompleteCount,
        invalidPositiveHoursCount,
        manualDayAdjustment,
        manualHourAdjustment,
        endingDayBalance,
        endingHourBalance,
        endingAdvancePendingBalance,
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

      [manualDayAdjustmentInput, manualHourAdjustmentInput].forEach((field) => {
        field?.addEventListener("input", recalculateRow);
        field?.addEventListener("change", recalculateRow);
      });

      row.querySelectorAll('[data-inventory-checkbox="true"]').forEach((field) => {
        field.addEventListener("change", confirmInventoryParticipation);
      });
    }

    recalculateRow();
  });

  const roleFilter = document.querySelector("[data-role-filter]");
  if (roleFilter) {
    const applyRoleFilter = () => {
      const selectedRole = String(roleFilter.value || "").trim().toLowerCase();
      rows.forEach((row) => {
        const rowRole = String(row.dataset.roleName || "").trim().toLowerCase();
        row.hidden = Boolean(selectedRole) && rowRole !== selectedRole;
      });
    };

    roleFilter.addEventListener("change", applyRoleFilter);
    scheduleForm.addEventListener("submit", () => {
      roleFilter.value = "";
      applyRoleFilter();
    });
    applyRoleFilter();
  }
}
