#include <stdio.h>
#include <stdlib.h>

int cmp_func(const void *x, const void *y) {
    return (*(int*)x - *(int*)y);
}

int main() {
    int size;
    scanf("%d", &size);
    int *nums = (int *)malloc(size * sizeof(int));

    if (size == 0) {
        printf("\n");
        return 0;
    }

    for (int k = 0; k < size; k++) {
        scanf("%d", &nums[k]);
    }

    qsort(nums, size, sizeof(int), cmp_func);

    for (int k = 0; k < size; k++) {
        printf("%d", nums[k]);
        if (k < size - 1) {
            printf(" ");
        }
    }
    printf("\n");

    free(nums);
    return 0;
}
