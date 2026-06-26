document.addEventListener("DOMContentLoaded", () => {
  initScheduleCalculations();
});

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
  const moneyHourModes = new Set(["pay_money", "pay_money_hours"]);
  const moneyDayModes = new Set(["pay_money_day"]);
  const compensationModesWithHours = new Set(["pay_hours", ...moneyHourModes]);

  const parseDecimal = (value) => {
    const normalized = String(value ?? "").trim().replace(",", ".");
    const parsed = Number.parseFloat(normalized);
    return Number.isFinite(parsed) ? parsed : 0;
  };

  const roundHours = (value) => Math.round((Number(value) + Number.EPSILON) * 100) / 100;

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
    dayReferenceHoursValue,
    weeklyTargetHoursValue,
  ) => {
    let remainingDayBalance = Math.max(roundHours(availableDayBalance), 0);
    let remainingHourBalance = Math.max(roundHours(availableHourBalance), 0);
    const normalizedWeeklyTargetHours = Math.max(roundHours(weeklyTargetHoursValue), 0);
    let cumulativeWorkedHours = 0;
    let cumulativeOvertimeHours = 0;

    let paymentDaysUsed = 0;
    let paymentDaysFromDayBalance = 0;
    let uncoveredPaymentDays = 0;
    let moneyPaymentDaysUsed = 0;
    let paymentHoursUsed = 0;
    let moneyPaymentHoursUsed = 0;
    const invalidPayDayIndices = [];
    const invalidPayMoneyDayIndices = [];
    const invalidPayHoursIndices = [];
    const invalidPayMoneyIndices = [];
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
          availableDayBalance: remainingDayBalance,
          availableHourBalance: remainingHourBalance,
          remainingDayBalance,
          remainingHourBalance,
          generatedDay: false,
          generatedHours: 0,
        };

        if (entry.mode === "pay_day") {
          paymentDaysUsed += 1;
          if (remainingDayBalance + 0.001 >= 1) {
            remainingDayBalance = roundHours(remainingDayBalance - 1);
            paymentDaysFromDayBalance += 1;
            dayState.source = "day_balance";
          } else {
            uncoveredPaymentDays += 1;
            invalidPayDayIndices.push(entry.index);
            dayState.source = "insufficient";
            dayState.valid = false;
          }
        } else if (moneyDayModes.has(entry.mode)) {
          moneyPaymentDaysUsed += 1;
          if (remainingDayBalance + 0.001 >= 1) {
            remainingDayBalance = roundHours(remainingDayBalance - 1);
            dayState.source = "day_balance";
          } else {
            invalidPayMoneyDayIndices.push(entry.index);
            dayState.source = "insufficient";
            dayState.valid = false;
          }
        } else if (entry.mode === "pay_hours") {
          paymentHoursUsed = roundHours(paymentHoursUsed + requestedHours);
          const remainingBefore = remainingHourBalance;
          remainingHourBalance = roundHours(remainingHourBalance - requestedHours);
          if (requestedHours <= 0.001 || remainingBefore + 0.001 < requestedHours) {
            invalidPayHoursIndices.push(entry.index);
            dayState.valid = false;
          }
          dayState.source = "hour_balance";
        } else if (moneyHourModes.has(entry.mode)) {
          moneyPaymentHoursUsed = roundHours(moneyPaymentHoursUsed + requestedHours);
          const remainingBefore = remainingHourBalance;
          remainingHourBalance = roundHours(remainingHourBalance - requestedHours);
          if (requestedHours <= 0.001 || remainingBefore + 0.001 < requestedHours) {
                invalidPayMoneyIndices.push(entry.index);
            dayState.valid = false;
          }
          dayState.source = "hour_balance";
        }

        if (specialGenerated && workedHours > 0.001) {
          remainingDayBalance = roundHours(remainingDayBalance + 1);
          dayState.generatedDay = true;
        }

        const previousOvertimeHours = cumulativeOvertimeHours;
        cumulativeWorkedHours = roundHours(cumulativeWorkedHours + workedHours);
        cumulativeOvertimeHours = roundHours(
          Math.max(cumulativeWorkedHours - normalizedWeeklyTargetHours, 0),
        );
        const generatedHours = roundHours(
          Math.max(cumulativeOvertimeHours - previousOvertimeHours, 0),
        );
        if (generatedHours > 0.001) {
          remainingHourBalance = roundHours(remainingHourBalance + generatedHours);
          dayState.generatedHours = generatedHours;
        }

        dayState.remainingDayBalance = remainingDayBalance;
        dayState.remainingHourBalance = remainingHourBalance;
        dayStates[entry.index] = dayState;
      });

    return {
      paymentDaysUsed,
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
      remainingDayBalance,
      remainingHourBalance,
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
  rows.forEach((row) => {
    const weeklyTarget = parseDecimal(row.dataset.weeklyTarget);
    const dailyMax = parseDecimal(row.dataset.dailyMax);
    const priorDayBalance = parseDecimal(row.dataset.priorDayBalance);
    const priorHourBalance = parseDecimal(row.dataset.priorHourBalance);
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

    const updatePaymentInfo = (dayCell, modeValue, state) => {
      const paymentInfo = dayCell.querySelector("[data-payment-info]");
      if (!paymentInfo) {
        return;
      }

      if (modeValue === "pay_day") {
        paymentInfo.hidden = false;
        if (state.paymentState?.source === "day_balance") {
          paymentInfo.textContent = `Pago dia: usa 1 dia acumulado. Saldo estimado tras este pago: ${formatHours(state.paymentState.remainingDayBalance)} dia(s).`;
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

      if (modeValue === "pay_hours") {
        paymentInfo.hidden = false;
        const coveredHours = state.dailyHours + state.compensationHoursValue;
        if (coveredHours >= dayReferenceHours - 0.001) {
          paymentInfo.textContent = `Trabajadas ${formatHours(state.dailyHours)} h + pagas ${formatHours(state.compensationHoursValue)} h = jornada cubierta.`;
        } else {
          paymentInfo.textContent = `Trabajadas ${formatHours(state.dailyHours)} h + pagas ${formatHours(state.compensationHoursValue)} h. Faltan ${formatHours(dayReferenceHours - coveredHours)} h.`;
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

    const updateBalanceNote = (endingDayBalance, endingHourBalance) => {
      if (!balanceNote) {
        return;
      }
      balanceNote.textContent = `Saldo previo: ${formatHours(priorDayBalance)} dia(s) y ${formatHours(priorHourBalance)} h. Resultado estimado: ${formatHours(endingDayBalance)} dia(s) y ${formatHours(endingHourBalance)} h.`;
    };

    const buildLiveSummary = (summaryState) => {
      const liveMessages = [];

      if (summaryState.specialDaysGenerated > 0.001) {
        liveMessages.push(`Genera ${formatHours(summaryState.specialDaysGenerated)} dia(s) por domingos/festivos.`);
      }
      if (summaryState.overtimeHours > 0.001) {
        liveMessages.push(`Extras calculadas: ${formatHours(summaryState.overtimeHours, true)}.`);
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
      if (summaryState.endingDayBalance < -0.001 || summaryState.endingHourBalance < -0.001) {
        liveMessages.push("Parte del saldo queda a favor de la empresa.");
      }

      if (showDetailedAlerts) {
        return liveMessages;
      }

      const conciseMessages = [];
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
        || summaryState.invalidHourDiscountCount > 0
        || summaryState.payHoursOverTargetCount > 0
        || summaryState.payMoneyOverTargetCount > 0
        || summaryState.invalidPositiveHoursCount > 0
      ) {
        conciseMessages.push("Revisa limites o saldo.");
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
      const dayStates = [];

      dayCells.forEach((dayCell) => {
        const dayIndex = Number.parseInt(dayCell.dataset.dayIndex || "0", 10);
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

        updateCompensationControl(compensationMode, compensationHours);

        const shift1 = shift1Select?.value || "";
        const shift2 = shift2Select?.value || "";
        const shift1Metrics = getShiftMetrics(shift1);
        const shift2Metrics = getShiftMetrics(shift2);
        const dailyHours = roundHours(shift1Metrics.hours + shift2Metrics.hours);
        const dailyNightHours = roundHours(shift1Metrics.night_hours + shift2Metrics.night_hours);
        const compensationHoursValue = roundHours(parseDecimal(compensationHours?.value));
        const specialGenerated = Boolean(dayCell.dataset.specialDay) && dailyHours > 0.001;
        const dailyOvertimeHours = roundHours(Math.max(dailyHours - dayReferenceHours, 0));

        totalHours = roundHours(totalHours + dailyHours);
        totalNightHours = roundHours(totalNightHours + dailyNightHours);

        if (specialGenerated) {
          specialDaysGenerated += 1;
        }

        if (modeValue === "pay_hours") {
          if (compensationHoursValue <= 0.001) {
            invalidPositiveHoursCount += 1;
          } else if (dailyHours + compensationHoursValue > dayReferenceHours + 0.001) {
            payHoursOverTargetCount += 1;
          } else if (dailyHours + compensationHoursValue < dayReferenceHours - 0.001) {
            payHoursIncompleteCount += 1;
          }
        } else if (moneyHourModes.has(modeValue)) {
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
        dayStates.push({
          dayIndex,
          dayCell,
          modeValue,
          compensationHoursValue,
          dailyHours,
          dailyOvertimeHours,
          specialGenerated,
        });
      });

      const manualDayAdjustment = roundHours(parseDecimal(manualDayAdjustmentInput?.value));
      const manualHourAdjustment = roundHours(parseDecimal(manualHourAdjustmentInput?.value));
      const overtimeHours = roundHours(Math.max(totalHours - effectiveWeeklyTarget, 0));
      const overtimeWeeklyRestrictionExceeded =
        hasOvertimeRestriction && overtimeHours > overtimeRestrictionWeeklyLimit + 0.001;
      const paymentUsage = resolvePaymentUsage(
        dayStates.map((dayState) => ({
          index: dayState.dayIndex,
          mode: dayState.modeValue,
          hours: dayState.compensationHoursValue,
          workedHours: dayState.dailyHours,
          specialGenerated: dayState.specialGenerated,
        })),
        priorDayBalance + manualDayAdjustment,
        priorHourBalance + manualHourAdjustment,
        dayReferenceHours,
        effectiveWeeklyTarget,
      );
      const endingDayBalance = roundHours(
        priorDayBalance
        + specialDaysGenerated
        + manualDayAdjustment
        - paymentUsage.paymentDaysUsed
        - paymentUsage.moneyPaymentDaysUsed,
      );
      const endingHourBalance = roundHours(
        priorHourBalance
        + overtimeHours
        + manualHourAdjustment
        - paymentUsage.paymentHoursUsed
        - paymentUsage.moneyPaymentHoursUsed,
      );

      dayStates.forEach((dayState) => {
        updatePaymentInfo(dayState.dayCell, dayState.modeValue, {
          dailyHours: dayState.dailyHours,
          compensationHoursValue: dayState.compensationHoursValue,
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

      updateBalanceNote(endingDayBalance, endingHourBalance);

      const liveMessages = buildLiveSummary({
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
        invalidHourDiscountCount: paymentUsage.invalidPayHoursIndices.length + paymentUsage.invalidPayMoneyIndices.length,
        payHoursOverTargetCount,
        payMoneyOverTargetCount,
        payHoursIncompleteCount,
        invalidPositiveHoursCount,
        manualDayAdjustment,
        manualHourAdjustment,
        endingDayBalance,
        endingHourBalance,
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
    }

    recalculateRow();
  });

  const roleFilter = document.querySelector("[data-role-filter]");
  const scheduleForm = document.querySelector(".stack-form");
  if (!roleFilter) {
    return;
  }

  const applyRoleFilter = () => {
    const selectedRole = String(roleFilter.value || "").trim().toLowerCase();
    rows.forEach((row) => {
      const rowRole = String(row.dataset.roleName || "").trim().toLowerCase();
      row.hidden = Boolean(selectedRole) && rowRole !== selectedRole;
    });
  };

  roleFilter.addEventListener("change", applyRoleFilter);
  scheduleForm?.addEventListener("submit", () => {
    roleFilter.value = "";
    applyRoleFilter();
  });
  applyRoleFilter();
}
