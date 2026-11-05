/* sort_bubble.c - reads n integers and prints them sorted (bubble sort). */
#include <stdio.h>
#include <stdlib.h>

/* Swaps two integers in place. */
static void swap(int *first, int *second)
{
    int temp = *first;
    *first = *second;
    *second = temp;
}

/* Sorts the array in ascending order; stops early once a pass makes no swap. */
static void bubble_sort(int *values, int count)
{
    for (int pass = 0; pass < count - 1; pass++) {
        int swapped = 0;
        for (int i = 0; i < count - 1 - pass; i++) {
            if (values[i] > values[i + 1]) {
                swap(&values[i], &values[i + 1]);
                swapped = 1;
            }
        }
        if (!swapped) {
            break;
        }
    }
}

int main(void)
{
    int count;

    if (scanf("%d", &count) != 1 || count < 0) {
        return 1;
    }
    int *values = malloc((count > 0 ? count : 1) * sizeof(int));
    if (values == NULL) {
        return 1;
    }
    for (int i = 0; i < count; i++) {
        if (scanf("%d", &values[i]) != 1) {
            free(values);
            return 1;
        }
    }
    bubble_sort(values, count);
    for (int i = 0; i < count; i++) {
        printf(i == 0 ? "%d" : " %d", values[i]);
    }
    printf("\n");
    free(values);
    return 0;
}
